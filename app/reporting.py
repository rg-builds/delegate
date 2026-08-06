"""Turn a finished call into one WhatsApp message for the user."""

from app import llm, telephony
from app.prompts import SUMMARIZER_PROMPT
from app.state import CallRecord

EMOJI = {"success": "✅", "failed": "❌", "declined": "❌", "timed-out": "⚠️"}

DEAD_STATUS_MESSAGES = {
    "busy": "❌ The line was busy. Want me to try again?",
    "no-answer": "❌ Nobody picked up. Want me to try again?",
    "failed": "❌ The call couldn't connect.",
    "canceled": "❌ The call was cancelled.",
}


async def report(record: CallRecord) -> None:
    """Notify the user about a finished call. Safe to call from multiple paths."""
    if not record.claim_report():
        return

    body = await _build_message(record)
    record.summary = body

    await telephony.send_whatsapp(record.user_wa_number, body)

    if record.status not in telephony.DEAD_STATUSES:
        record.status = "done"


async def _build_message(record: CallRecord) -> str:
    who = record.callee_name or record.to_number

    # Call never connected, so there's nothing to summarize.
    if record.status in DEAD_STATUS_MESSAGES and not record.transcript:
        return f"{DEAD_STATUS_MESSAGES[record.status]}\n\nTask: {record.task}"

    # The agent told us the outcome itself via the end_call tool.
    if record.outcome and record.tool_summary:
        emoji = EMOJI.get(record.outcome, "⚠️")
        return f"{emoji} {record.tool_summary}"[:600]

    if not record.transcript:
        return f"⚠️ I called {who} but got no usable conversation.\n\nTask: {record.task}"

    try:
        user_content = (
            f"TASK: {record.task}\n"
            f"CALLED: {who}\n"
            f"CALL STATUS: {record.status}\n\n"
            f"TRANSCRIPT:\n{record.transcript_text()}"
        )
        summary = await llm.complete(SUMMARIZER_PROMPT, user_content)
        return str(summary).strip()[:600]
    except Exception as e:
        print(f"summarize error: {e}")
        return (
            f"⚠️ I called {who} but couldn't summarize the result.\n\n"
            f"Last thing said: {record.transcript[-1]['text'][:200]}"
        )
