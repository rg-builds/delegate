"""In-memory call registry, keyed by Twilio Call SID.

The Call SID is the correlation key across every layer: it's returned when the
call is placed, echoed in the media stream `start` event, and sent on status
callbacks. Swap this for SQLite when call history is wanted.
"""

from dataclasses import dataclass, field


@dataclass
class CallRecord:
    call_sid: str
    user_wa_number: str              # "whatsapp:+91..."
    to_number: str
    task: str
    callee_name: str | None = None
    context: str | None = None
    status: str = "queued"           # queued|ringing|in-progress|done|failed|busy|no-answer|timed-out
    transcript: list[dict] = field(default_factory=list)
    outcome: str | None = None       # "success"|"failed"|"declined", from the end_call tool
    tool_summary: str | None = None  # the agent's own summary, from the end_call tool
    summary: str | None = None       # the message actually sent to the user
    reported: bool = False           # ensures the user is notified exactly once

    def add_turn(self, role: str, text: str) -> None:
        text = text.strip()
        if text:
            self.transcript.append({"role": role, "text": text})

    def transcript_text(self) -> str:
        return "\n".join(f"{t['role']}: {t['text']}" for t in self.transcript)

    def claim_report(self) -> bool:
        """Return True exactly once, for whichever path reports first."""
        if self.reported:
            return False
        self.reported = True
        return True


class CallRegistry:
    def __init__(self) -> None:
        self._calls: dict[str, CallRecord] = {}

    def add(self, record: CallRecord) -> None:
        self._calls[record.call_sid] = record

    def get(self, call_sid: str | None) -> CallRecord | None:
        if not call_sid:
            return None
        return self._calls.get(call_sid)

    def set_status(self, call_sid: str, status: str) -> CallRecord | None:
        record = self.get(call_sid)
        if record:
            record.status = status
        return record


registry = CallRegistry()
