import pytest

from app import reporting
from app.state import CallRecord


def make_record(**overrides) -> CallRecord:
    defaults = dict(
        call_sid="CA123",
        user_wa_number="whatsapp:+918837557003",
        to_number="+911234567890",
        task="ask if they are open on sunday",
    )
    defaults.update(overrides)
    return CallRecord(**defaults)


@pytest.fixture
def sent(monkeypatch):
    messages = []

    async def fake_send(to, body):
        messages.append((to, body))

    monkeypatch.setattr(reporting.telephony, "send_whatsapp", fake_send)
    return messages


@pytest.mark.asyncio
async def test_reports_exactly_once(sent):
    record = make_record(outcome="success", tool_summary="They are open 9 to 5.")

    await reporting.report(record)
    await reporting.report(record)

    assert len(sent) == 1


@pytest.mark.asyncio
async def test_uses_tool_summary_when_available(sent):
    record = make_record(outcome="success", tool_summary="Open 9am to 5pm on Sunday.")

    await reporting.report(record)

    _, body = sent[0]
    assert body.startswith("✅")
    assert "9am to 5pm" in body
    assert record.status == "done"


@pytest.mark.asyncio
async def test_declined_outcome_uses_cross(sent):
    record = make_record(outcome="declined", tool_summary="Wrong number.")

    await reporting.report(record)

    assert sent[0][1].startswith("❌")


@pytest.mark.asyncio
async def test_no_answer_skips_summarizer(sent, monkeypatch):
    async def boom(*args, **kwargs):
        raise AssertionError("summarizer must not run without a transcript")

    monkeypatch.setattr(reporting.llm, "complete", boom)

    record = make_record(status="no-answer")
    await reporting.report(record)

    body = sent[0][1]
    assert body.startswith("❌")
    assert "picked up" in body


@pytest.mark.asyncio
async def test_summarizes_transcript_when_no_tool_outcome(sent, monkeypatch):
    async def fake_complete(system, user, **kwargs):
        assert "open on sunday" in user
        return "✅ They're open Sunday 10am to 6pm."

    monkeypatch.setattr(reporting.llm, "complete", fake_complete)

    record = make_record()
    record.add_turn("delegate", "Are you open on Sunday?")
    record.add_turn("callee", "Yes, 10 to 6.")

    await reporting.report(record)

    assert "10am to 6pm" in sent[0][1]


@pytest.mark.asyncio
async def test_summarizer_failure_still_notifies_user(sent, monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("api down")

    monkeypatch.setattr(reporting.llm, "complete", boom)

    record = make_record()
    record.add_turn("callee", "We close at six.")

    await reporting.report(record)

    assert sent[0][1].startswith("⚠️")


@pytest.mark.asyncio
async def test_bridge_produced_no_transcript(sent):
    record = make_record(status="in-progress")

    await reporting.report(record)

    assert sent[0][1].startswith("⚠️")


class TestCallRecord:
    def test_blank_turns_are_ignored(self):
        record = make_record()
        record.add_turn("callee", "   ")
        assert record.transcript == []

    def test_transcript_text_formats_roles(self):
        record = make_record()
        record.add_turn("delegate", "Hello")
        record.add_turn("callee", "Hi")
        assert record.transcript_text() == "delegate: Hello\ncallee: Hi"

    def test_claim_report_is_single_use(self):
        record = make_record()
        assert record.claim_report() is True
        assert record.claim_report() is False
