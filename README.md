# Delegate

Delegate is an AI representative that makes phone calls on your behalf. You text
it a task on WhatsApp, it phones the person, holds the conversation, and reports
back what happened.

```
You (WhatsApp)                    "call 98xxx and ask if they're open Sunday"
      |
      v
FastAPI  /webhook/whatsapp        parse the request with an LLM
      |
      v
Twilio REST API                   place the outbound call
      |
      v
Twilio Media Streams  <-------->  /media-stream  <-------->  OpenAI Realtime
      (mu-law 8kHz audio)          (bridge + tools)          (speech-to-speech)
      |
      v
You (WhatsApp)                    "They're open Sunday 10am to 6pm."
```

## How it works

There are two WebSockets, with this service in the middle:

```
Twilio  <====>  Delegate  <====>  OpenAI Realtime
        WS #1             WS #2
```

Twilio owns the phone call. Delegate forwards audio between the two sides and
adds the business logic: task parsing, transcripts, tool handling, reporting.

**Audio passes through untouched.** OpenAI Realtime speaks G.711 mu-law at 8kHz
(`audio/pcmu`), which is exactly what Twilio Media Streams sends. No decoding,
no resampling, no re-encoding in either direction.

**One model does everything.** `gpt-realtime` is speech-to-speech, so there is no
STT -> LLM -> TTS chain. The `transcription` setting runs a *separate* model
purely to produce text for logs and the summary fallback; it does not feed the
conversation.

## Setup

Requires Python 3.12+ and a Twilio account with the WhatsApp Sandbox enabled.

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env      # then fill it in
```

Expose the server publicly so Twilio can reach it:

```bash
ngrok http 8000
```

Put the resulting URL in `.env` as `BASE_URL`, then point your Twilio WhatsApp
Sandbox webhook at `{BASE_URL}/webhook/whatsapp`.

> ngrok URLs change on every restart. Update both `BASE_URL` and the Twilio
> console when that happens, or signature validation will reject every request.

Run it:

```bash
.venv/bin/uvicorn app.main:app --reload
```

Then WhatsApp yourself something like:

```
call 9876543210 and ask if they are open on Sunday
```

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `TWILIO_ACCOUNT_SID` | — | Twilio credentials |
| `TWILIO_AUTH_TOKEN` | — | Also used to verify webhook signatures |
| `TWILIO_PHONE_NUMBER` | — | The number calls are placed *from* |
| `TWILIO_WHATSAPP_NUMBER` | sandbox | Number WhatsApp replies are sent from |
| `OPENAI_API_KEY` | — | |
| `BASE_URL` | — | Public HTTPS URL of this service |
| `OPENAI_REALTIME_MODEL` | `gpt-realtime-2.1` | The conversation model |
| `OPENAI_TEXT_MODEL` | `gpt-5-mini` | Parsing and summarizing |
| `OPENAI_TRANSCRIBE_MODEL` | `gpt-4o-transcribe` | Transcript text only |
| `OPENAI_VOICE` | `marin` | See voices below |
| `MAX_CALL_SECONDS` | `300` | Hard cap; call is hung up after this |
| `ALLOWED_WHATSAPP_NUMBERS` | empty | Comma-separated allowlist. **Empty allows anyone** |
| `SKIP_SIGNATURE_VALIDATION` | `0` | Set `1` only for local testing |
| `DEBUG_AUDIO` | `0` | Verbose audio/VAD logging |

### Voice options

Verified working with `gpt-realtime-2.1`:

```
alloy  echo  shimmer  coral  verse  ballad  ash  sage  marin  cedar
```

`fable`, `onyx` and `nova` exist in the TTS API but are rejected by Realtime.
`marin` and `cedar` are the newest and handle mid-sentence language switching
best.

### Tuning voice activity detection

Phone audio is 8kHz and lossy, so turn detection needs tuning. These matter more
than they look:

| Variable | Default | Effect |
|---|---|---|
| `VAD_THRESHOLD` | `0.4` | Higher = agent interrupts itself less, but may stop hearing quiet callers entirely |
| `VAD_PREFIX_PADDING_MS` | `500` | Audio kept *before* speech is detected. Too low clips the first syllable, so "6 baje" is heard as just "baje" |
| `VAD_SILENCE_MS` | `600` | How long a pause ends the caller's turn |
| `VAD_MODE` | `server` | `semantic` judges whether a thought finished, rather than reacting to volume |
| `NOISE_REDUCTION` | `off` | `near_field` / `far_field`. Can over-attenuate quiet phone audio |

## Project layout

```
app/
  main.py         routes only
  config.py       env vars, with a fail-fast startup check
  parsing.py      WhatsApp message -> CallRequest, via one LLM call
  telephony.py    Twilio REST: place_call, hangup_call, send_whatsapp
  realtime.py     Twilio <-> OpenAI bridge, end_call tool, barge-in
  reporting.py    transcript -> the WhatsApp message you receive
  prompts.py      system instruction, parser and summarizer prompts
  state.py        CallRecord / CallRegistry, keyed by Twilio Call SID
  llm.py          minimal chat-completions client
  security.py     signature validation and sender allowlist
tests/
```

No SDKs for Twilio or OpenAI. Everything is raw HTTP and WebSockets, so the
protocols stay visible.

### Endpoints

| Route | Purpose |
|---|---|
| `POST /webhook/whatsapp` | Inbound message. Parses and places the call |
| `POST /voice` | Returns TwiML connecting the call to the WebSocket |
| `WS /media-stream` | The audio bridge |
| `POST /webhook/call-status` | Twilio lifecycle events |
| `GET /health` | Liveness |

## Design notes

**Call SID is the correlation key.** Twilio returns it when the call is placed,
echoes it in the media stream `start` event, and sends it on status callbacks.
Everything is keyed by it.

**The agent ends its own call.** An `end_call` tool takes an outcome
(`success` / `failed` / `declined`) and a summary. The agent says goodbye, then
Delegate hangs up via the REST API. If the agent's summary is available it is
used directly; otherwise the transcript is summarized as a fallback.

**Hangup waits for playback.** `response.done` means OpenAI finished
*generating*, not that Twilio finished *playing* — generation outruns realtime.
Delegate sends a Twilio `mark` and waits for it to echo back before hanging up,
so the goodbye isn't cut off.

**Reporting runs exactly once.** Two paths can finish a call: the bridge
returning, or the `completed` status callback. `CallRecord.claim_report()` makes
the first one win, so you never get two WhatsApp messages.

**Numbers are confirmed, not assumed.** At 8kHz the frequencies that distinguish
spoken digits are largely gone before the audio arrives, so the agent is
instructed to repeat times and amounts back before recording them, and is
explicitly forbidden from substituting an option it suggested itself.

## Security

- Every webhook verifies the `X-Twilio-Signature` HMAC. Invalid requests get 403.
- `ALLOWED_WHATSAPP_NUMBERS` restricts who can trigger calls.

> `ALLOWED_WHATSAPP_NUMBERS` defaults to empty, which permits **anyone** whose
> request passes signature validation. Set it before exposing this publicly —
> placing calls costs money.

## Tests

```bash
.venv/bin/python -m pytest
```

Covers parsing, reporting, the signature and allowlist gates, session config,
and the media-stream endpoint. A fake Twilio client replays `start`/`media`/`stop`
frames against the real endpoint with the bridge stubbed, so no phone call or
OpenAI connection is needed.

## Known limitations

**Telephony audio is the ceiling.** Calls are 8kHz mu-law end to end. ChatGPT
voice mode sounds better because it gets 24kHz from your device mic — roughly
six times the data, with the high frequencies that distinguish consonants and
digits intact. Nothing configurable closes that gap while dialling real phones.

**State is in memory.** `CallRegistry` is a dict, so it resets on restart and
won't work across multiple processes. Fine for a single instance; needs SQLite
before scaling out.

**One task per message.** Multi-turn clarification isn't wired up yet — if the
request is ambiguous, Delegate asks you to resend the full request.
