# Delegate — Implementation Plan

Delegate is an AI representative you text on WhatsApp. It parses your request, places a phone call via Twilio, holds the conversation using OpenAI Realtime, and reports the outcome back to you on WhatsApp.

**Provider decision: OpenAI Realtime only.** The existing Gemini path is legacy and will be removed. OpenAI Realtime speaks G.711 mu-law 8kHz natively (`audio/pcmu`), which is exactly what Twilio Media Streams uses — no resampling, no `audioop` (removed in Python 3.13).

---

## 1. Current State

Working end-to-end skeleton on branch `main`:

| Piece | File | Status |
|---|---|---|
| WhatsApp inbound webhook | `app/main.py` `/webhook/whatsapp` | ✅ works |
| Message → (number, task) parsing | `app/main.py` `parse_request()` | ⚠️ regex, brittle |
| Twilio outbound call + TwiML | `app/main.py` `/voice` | ✅ works |
| Twilio ↔ OpenAI Realtime audio bridge | `app/openai_realtime.py` | ✅ works (pcmu passthrough, barge-in, transcripts) |
| Gemini bridge | `app/main.py`, `app/gemini.py` | 🗑️ to be removed |
| Task state | in-memory `pending_tasks` dict | ✅ fine for single process |
| Result reporting to user | — | ❌ missing (transcript is only printed) |
| Call lifecycle (hangup, timeouts, no-answer) | — | ❌ missing |
| Security (signature validation, allowlist) | — | ❌ missing |

Env vars already configured in `.env`: `OPENAI_API_KEY`, `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`, `NGROK_URL`. Add: `PROVIDER=openai` (until Gemini path is deleted), `ALLOWED_WHATSAPP_NUMBERS`, `TWILIO_WHATSAPP_NUMBER`.

---

## 2. Architecture Layers

```
WhatsApp user
     │  (text message)
     ▼
[L1 Ingress]        Twilio WhatsApp webhook → validate signature → sender allowlist
     │
     ▼
[L2 Understanding]  LLM parse: {to_number, callee_name, task, context}
     │                 ├─ ambiguous/missing number → WhatsApp clarification reply, stop
     │                 └─ clear → continue
     ▼
[L3 Telephony]      Twilio REST: place call, register status callbacks
     │                 └─ busy / no-answer / failed → WhatsApp notification
     ▼
[L4 Voice Agent]    Twilio Media Stream ⇄ OpenAI Realtime (audio/pcmu passthrough)
     │                 ├─ system prompt built from task
     │                 ├─ end_call(outcome, summary) tool → hang up via Twilio REST
     │                 └─ hard max-duration timeout
     ▼
[L5 Reporting]      transcript → summarization call → WhatsApp result message
     │
[L6 State]          calls registry keyed by call_sid:
                    {user_wa_number, task, status, transcript, outcome}
[L7 Infra]          uvicorn + ngrok (dev) → Railway/Fly (deploy); NGROK_URL becomes BASE_URL
```

---

## 3. Requirements

### R1 — WhatsApp feedback loop
**User story:** As the user, I want WhatsApp replies at every stage, so I know what happened without watching server logs.

Acceptance criteria:
1. WHEN a call is successfully queued THEN the user receives "📞 Calling {number}: {task}".
2. WHEN a call ends normally THEN the user receives an outcome summary within ~15 seconds of hangup.
3. WHEN a call fails, is busy, or goes unanswered THEN the user receives a message naming the failure.
4. IF the Twilio call-creation API returns an error THEN the user is told the call could not be placed.

### R2 — Reliable task parsing
**User story:** As the user, I want to write natural messages ("call +91… and book a table for 2 at 8pm"), so I don't need a rigid format.

Acceptance criteria:
1. WHEN the message contains a phone number and a task THEN an LLM extracts `{to_number (E.164), callee_name?, task, context?}`.
2. IF no phone number is found THEN Delegate replies asking for the number and does NOT place a call. (Remove `DEFAULT_TO_NUMBER` fallback — no silent calls to a hardcoded number.)
3. IF the task is empty or ambiguous THEN Delegate replies asking for clarification.
4. WHEN the message contains other digit sequences (prices, times, order numbers) THEN they are not mistaken for the phone number.
5. Bare 10-digit Indian mobile numbers are normalized to `+91…`.

### R3 — Call lifecycle control
**User story:** As the user, I want calls to end cleanly when the task is done or impossible, so I don't burn minutes or trap the callee.

Acceptance criteria:
1. The Realtime session exposes an `end_call(outcome: "success" | "failed" | "declined", summary: str)` tool.
2. WHEN the model calls `end_call` THEN the agent says a short goodbye, the OpenAI socket closes, and the call is hung up via Twilio REST (`Status=completed`).
3. Every call has a hard max duration (default 5 min, env-configurable) enforced with an asyncio timeout; on expiry the call is hung up and the outcome is "timed out".
4. WHEN the callee hangs up first THEN the bridge shuts down cleanly and reporting still runs.
5. Call `StatusCallback` events (initiated/ringing/answered/completed + failed/busy/no-answer) are received on `/webhook/call-status` and update the call record.

### R4 — Outcome reporting
**User story:** As the user, I want a concise result ("✅ Table booked for 2 at 8pm, under your name"), so the loop is closed.

Acceptance criteria:
1. WHEN a call completes THEN the full transcript is summarized by one LLM call into: outcome status, key facts obtained, and any follow-up needed.
2. The summary prefers the `end_call` tool's own `outcome`/`summary` when available; the transcript summarization is the fallback.
3. The WhatsApp result message is ≤ ~600 chars, leads with ✅/❌/⚠️, and contains the concrete answer (price, time, confirmation number) when one was obtained.

### R5 — Security
**User story:** As the operator, I want only me to be able to trigger calls, so the bot can't be abused for spam calls on my Twilio account.

Acceptance criteria:
1. Inbound webhooks validate the `X-Twilio-Signature` header (twilio SDK `RequestValidator`); invalid → 403.
2. `From` must be in `ALLOWED_WHATSAPP_NUMBERS` (comma-separated env var); otherwise the message is ignored (no reply, no call).
3. No secrets are logged; transcripts are logged only at debug level.

### R6 — Single provider (OpenAI)
1. `PROVIDER` switch, Gemini bridge, `app/gemini.py`, and all `audioop` usage are removed.
2. `google-genai` is dropped from `requirements.txt`.
3. The system prompt (currently `build_system_instruction`) moves next to the Realtime code and gains a "when the task is complete or clearly impossible, call end_call" instruction.

---

## 4. Design

### 4.1 Module layout

```
app/
  main.py            FastAPI app: routes only (whatsapp webhook, /voice, /media-stream, /webhook/call-status, /health)
  parsing.py         LLM-based message → CallRequest extraction (OpenAI chat completions, JSON mode)
  telephony.py       Twilio REST helpers: place_call(), hangup_call(), send_whatsapp()
  realtime.py        (renamed from openai_realtime.py) Realtime bridge + end_call tool handling
  prompts.py         build_system_instruction(task), parser prompt, summarizer prompt
  reporting.py       summarize_transcript() → WhatsApp result message
  state.py           CallRegistry: dict[call_sid, CallRecord]; CallRecord dataclass
```

### 4.2 Key flows

**Happy path**
1. `/webhook/whatsapp`: validate signature → allowlist → `parsing.parse(message)` → `CallRequest`.
2. `telephony.place_call(to, twiml_url, status_callback_url)` → `call_sid`; store `CallRecord(user_wa, task, status="queued")`; reply "📞 Calling…".
3. `/voice` returns `<Connect><Stream url="wss://{BASE}/media-stream"/>`.
4. `/media-stream`: consume `start` event → look up record by `callSid` → `realtime.handle_media_stream(ws, stream_sid, record)`.
5. Bridge runs; transcripts append to `record.transcript`; model calls `end_call` → goodbye → `hangup_call(call_sid)`.
6. After bridge exits (any reason): `reporting.summarize(record)` → `send_whatsapp(record.user_wa, result)` → `record.status="done"`. Guard so reporting runs exactly once (either from the bridge exit or the `completed` status callback, whichever fires — not both).

**Failure paths**
- Parse needs clarification → WhatsApp question, no call, stop.
- Twilio create-call error → "❌ Couldn't place the call: {reason}".
- Status callback `busy`/`no-answer`/`failed` → "❌ {callee} didn't pick up / line busy" (and no summarization, since there's no transcript).
- Bridge exception → still run reporting with whatever transcript exists, message leads with ⚠️.

### 4.3 end_call tool (OpenAI Realtime)

In `session.update`, add:
```json
"tools": [{
  "type": "function",
  "name": "end_call",
  "description": "Call when the task is complete, or clearly cannot be completed. After calling this, say one short goodbye sentence.",
  "parameters": {
    "type": "object",
    "properties": {
      "outcome": {"type": "string", "enum": ["success", "failed", "declined"]},
      "summary": {"type": "string", "description": "One or two sentences: what was accomplished or why it failed, including any concrete details (times, prices, confirmation numbers)."}
    },
    "required": ["outcome", "summary"]
  }
}]
```
Handling in the OpenAI listener: on `response.function_call_arguments.done` for `end_call` → store outcome on the record → send `conversation.item.create` (function output "acknowledged") + `response.create` so the model speaks its goodbye → wait for `response.done` (or ~3s) → `hangup_call(call_sid)`.

### 4.4 Parsing (L2)

One `chat.completions` call (small model, JSON schema output):
```
Input: raw WhatsApp text
Output: {"to_number": "+91…" | null, "callee_name": str | null,
         "task": str, "context": str | null,
         "clarification_needed": str | null}
```
If `clarification_needed` or `to_number` is null → send the clarification question back on WhatsApp; keep the pending message text in memory keyed by user number so their next reply can be merged (nice-to-have; v1 may simply ask them to resend the full request).

### 4.5 State (L6)

```python
@dataclass
class CallRecord:
    call_sid: str
    user_wa_number: str      # "whatsapp:+91…"
    to_number: str
    task: str
    status: str              # queued|ringing|in-progress|done|failed|busy|no-answer|timed-out
    transcript: list[dict]   # [{"role","text"}]
    outcome: str | None      # from end_call or summarizer
    summary: str | None
    reported: bool = False   # reporting-ran-once guard
```
In-memory `dict[str, CallRecord]` for MVP. SQLite later if history is wanted.

---

## 5. Tasks

- [ ] **T1. Restructure & de-Gemini** *(R6)*
  - [ ] 1.1 Delete `app/gemini.py`, the Gemini branch in `/media-stream`, `PROVIDER` switch, all `audioop`/`google-genai` usage; update `requirements.txt`.
  - [ ] 1.2 Split `main.py` into the module layout in §4.1 (`telephony.py`, `prompts.py`, `state.py`; rename `openai_realtime.py` → `realtime.py`). Routes stay thin.
  - [ ] 1.3 Verify: server boots, `/health` ok, an end-to-end test call still works.

- [ ] **T2. WhatsApp outbound + call placement feedback** *(R1.1, R1.4)*
  - [ ] 2.1 `telephony.send_whatsapp(to, body)` using Twilio Messages API (`From=whatsapp:{TWILIO_WHATSAPP_NUMBER}`).
  - [ ] 2.2 Reply "📞 Calling {number}: {task}" on successful queue; "❌ Couldn't place the call" on Twilio error.
  - [ ] 2.3 Create `CallRecord` at queue time (replaces `pending_tasks`).

- [ ] **T3. Call lifecycle** *(R3)*
  - [ ] 3.1 Add `StatusCallback`/`StatusCallbackEvent` to call creation; implement `/webhook/call-status`; update record status; WhatsApp-notify on `busy`/`no-answer`/`failed`. *(R1.3)*
  - [ ] 3.2 `telephony.hangup_call(call_sid)` (Twilio REST `Status=completed`).
  - [ ] 3.3 `end_call` tool per §4.3: session tools config, function-call handling, goodbye-then-hangup sequence.
  - [ ] 3.4 Max-duration timeout (`MAX_CALL_SECONDS`, default 300) wrapping the bridge tasks.

- [ ] **T4. Outcome reporting** *(R4, R1.2)*
  - [ ] 4.1 `reporting.summarize(record)`: use `end_call` outcome if present, else one summarization call over the transcript.
  - [ ] 4.2 Send WhatsApp result (✅/❌/⚠️ + concrete facts, ≤600 chars) after bridge exit; `reported` guard so it runs exactly once.
  - [ ] 4.3 Failure-path variants (no answer → no summarizer call; bridge crash → ⚠️ with partial transcript).

- [ ] **T5. LLM parsing** *(R2)*
  - [ ] 5.1 `parsing.py` with JSON-schema extraction per §4.4; delete `parse_request` regex and `DEFAULT_TO_NUMBER`.
  - [ ] 5.2 Clarification replies when number/task missing or ambiguous.
  - [ ] 5.3 Unit tests: number+task, number-with-noise-digits, bare 10-digit Indian number, no number, empty task.

- [ ] **T6. Security** *(R5)*
  - [ ] 6.1 Twilio signature validation middleware/dependency for both webhooks (dev bypass flag for local testing without ngrok).
  - [ ] 6.2 `ALLOWED_WHATSAPP_NUMBERS` allowlist check; silently drop others.

- [ ] **T7. Prompt polish** *(R6.3)*
  - [ ] 7.1 Move `build_system_instruction` to `prompts.py`; add end_call guidance, callee-name usage, "never reveal you're waiting on tools" phrasing.
  - [ ] 7.2 Live test calls: booking scenario, info-gathering scenario, refusal scenario ("they say wrong number").

- [ ] **T8. Deploy (later)** *(L7)*
  - [ ] 8.1 Rename `NGROK_URL` → `BASE_URL`; Procfile/Dockerfile; deploy to Railway/Fly (websocket support required); point Twilio webhooks at it.

**Order:** T1 → T2 → T3 → T4 ship the product loop. T5/T6 harden it. T7 improves quality. T8 when ready to run without a laptop.

---

## 6. Testing Strategy

- **Unit:** parsing (T5.3), summarizer prompt with canned transcripts, TwiML generation.
- **Integration (no phone):** fake Twilio Media Stream websocket client replaying recorded `start`/`media`/`stop` frames against `/media-stream`; mock OpenAI socket for tool-call handling.
- **Live smoke test per milestone:** WhatsApp yourself → call your own second number → confirm WhatsApp summary arrives. This is the real acceptance test; do it after T2, T3, and T4.

## 7. Open Questions / Later Ideas

- Multi-turn clarification memory (v1 asks user to resend full request).
- Answering machine detection (`MachineDetection=Enable`) — skip voicemails or leave a message?
- Mid-call user updates ("also ask if they have parking") — would need WhatsApp → live session injection.
- Call history + cost tracking (SQLite) once volume justifies it.
- Multiple concurrent calls per user — registry already supports it; WhatsApp threading is the UX question.
