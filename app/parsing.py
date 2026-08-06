"""WhatsApp message -> CallRequest, via one LLM extraction call."""

import re
from dataclasses import dataclass

from app import llm
from app.prompts import PARSER_PROMPT

SCHEMA = {
    "type": "object",
    "properties": {
        "to_number": {"type": ["string", "null"]},
        "callee_name": {"type": ["string", "null"]},
        "task": {"type": ["string", "null"]},
        "context": {"type": ["string", "null"]},
        "clarification_needed": {"type": ["string", "null"]},
    },
    "required": ["to_number", "callee_name", "task", "context", "clarification_needed"],
    "additionalProperties": False,
}


@dataclass
class CallRequest:
    to_number: str | None = None
    callee_name: str | None = None
    task: str | None = None
    context: str | None = None
    clarification_needed: str | None = None

    @property
    def is_actionable(self) -> bool:
        return bool(self.to_number and self.task and not self.clarification_needed)


def normalize_e164(raw: str | None) -> str | None:
    """Digits -> E.164. Bare 10-digit numbers are treated as Indian mobiles."""
    if not raw:
        return None

    digits = re.sub(r"\D", "", raw)

    if len(digits) == 10:
        digits = "91" + digits

    # Shortest realistic international number is ~8 digits after country code
    if not 10 <= len(digits) <= 15:
        return None

    return "+" + digits


async def parse(message: str) -> CallRequest:
    if not message or not message.strip():
        return CallRequest(
            clarification_needed="Send me who to call and what to ask, and I'll handle it."
        )

    try:
        data = await llm.complete(PARSER_PROMPT, message, schema=SCHEMA)
    except Exception as e:
        print(f"parse error: {e}")
        return CallRequest(
            clarification_needed="I couldn't read that. Try: call +91XXXXXXXXXX and ask ..."
        )

    request = CallRequest(
        to_number=normalize_e164(data.get("to_number")),
        callee_name=data.get("callee_name"),
        task=(data.get("task") or "").strip() or None,
        context=data.get("context"),
        clarification_needed=data.get("clarification_needed"),
    )

    # The model may return a task but an unusable number, or vice versa.
    if not request.to_number and not request.clarification_needed:
        request.clarification_needed = "Which number should I call? Send it with the country code."
    elif not request.task and not request.clarification_needed:
        request.clarification_needed = "What should I ask them?"

    return request
