import pytest

from app.parsing import CallRequest, normalize_e164, parse


class TestNormalizeE164:
    def test_bare_indian_mobile_gets_country_code(self):
        assert normalize_e164("8837557003") == "+918837557003"

    def test_already_e164_passes_through(self):
        assert normalize_e164("+918837557003") == "+918837557003"

    def test_strips_formatting(self):
        assert normalize_e164("+91 88375-57003") == "+918837557003"

    def test_idempotent(self):
        once = normalize_e164("8837557003")
        assert normalize_e164(once) == once

    def test_rejects_too_short(self):
        assert normalize_e164("42") is None

    def test_rejects_too_long(self):
        assert normalize_e164("1234567890123456789") is None

    def test_handles_none_and_empty(self):
        assert normalize_e164(None) is None
        assert normalize_e164("") is None


class TestCallRequest:
    def test_actionable_needs_number_and_task(self):
        assert CallRequest(to_number="+911234567890", task="ask hours").is_actionable

    def test_not_actionable_without_number(self):
        assert not CallRequest(task="ask hours").is_actionable

    def test_not_actionable_without_task(self):
        assert not CallRequest(to_number="+911234567890").is_actionable

    def test_not_actionable_when_clarification_pending(self):
        request = CallRequest(
            to_number="+911234567890", task="ask hours", clarification_needed="which branch?"
        )
        assert not request.is_actionable


@pytest.mark.asyncio
async def test_empty_message_asks_for_details_without_calling_llm():
    result = await parse("   ")
    assert not result.is_actionable
    assert result.clarification_needed


@pytest.mark.asyncio
async def test_missing_number_produces_clarification(monkeypatch):
    async def fake_complete(*args, **kwargs):
        return {
            "to_number": None,
            "callee_name": "Joe's Pizza",
            "task": "ask if they are open sunday",
            "context": None,
            "clarification_needed": None,
        }

    monkeypatch.setattr("app.parsing.llm.complete", fake_complete)

    result = await parse("call Joe's Pizza and ask if they are open sunday")
    assert not result.is_actionable
    assert "number" in result.clarification_needed.lower()


@pytest.mark.asyncio
async def test_noise_digits_are_not_used_as_number(monkeypatch):
    """The model must not mistake a table size or time for a phone number."""

    async def fake_complete(*args, **kwargs):
        return {
            "to_number": "+918837557003",
            "callee_name": None,
            "task": "book a table for 2 at 8pm",
            "context": "party of 2, 8pm",
            "clarification_needed": None,
        }

    monkeypatch.setattr("app.parsing.llm.complete", fake_complete)

    result = await parse("call 8837557003 and book a table for 2 at 8pm")
    assert result.to_number == "+918837557003"
    assert result.is_actionable


@pytest.mark.asyncio
async def test_llm_failure_degrades_gracefully(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("api down")

    monkeypatch.setattr("app.parsing.llm.complete", boom)

    result = await parse("call someone")
    assert not result.is_actionable
    assert result.clarification_needed
