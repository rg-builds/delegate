"""Webhook tests, focused on: nothing places a call unless it should."""

import pytest
from fastapi.testclient import TestClient

from app import config, main, security
from app.parsing import CallRequest


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def spy(monkeypatch):
    """Capture calls placed and messages sent, without touching Twilio."""
    state = {"calls": [], "messages": []}

    async def fake_place_call(to_number):
        state["calls"].append(to_number)
        return "CA_fake"

    async def fake_send(to, body):
        state["messages"].append((to, body))

    monkeypatch.setattr(main.telephony, "place_call", fake_place_call)
    monkeypatch.setattr(main.telephony, "send_whatsapp", fake_send)
    return state


@pytest.fixture
def allow_signatures(monkeypatch):
    monkeypatch.setattr(config, "SKIP_SIGNATURE_VALIDATION", True)


@pytest.fixture
def open_allowlist(monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_WHATSAPP_NUMBERS", set())


def set_parse_result(monkeypatch, **kwargs):
    async def fake_parse(message):
        return CallRequest(**kwargs)

    monkeypatch.setattr(main.parsing, "parse", fake_parse)


FORM = {"From": "whatsapp:+918837557003", "Body": "call +911234567890 and ask hours"}


class TestSignatureGate:
    def test_unsigned_request_is_rejected_and_places_no_call(self, client, spy, monkeypatch):
        monkeypatch.setattr(config, "SKIP_SIGNATURE_VALIDATION", False)

        response = client.post("/webhook/whatsapp", data=FORM)

        assert response.status_code == 403
        assert spy["calls"] == []
        assert spy["messages"] == []

    def test_bad_signature_is_rejected(self, client, spy, monkeypatch):
        monkeypatch.setattr(config, "SKIP_SIGNATURE_VALIDATION", False)

        response = client.post(
            "/webhook/whatsapp",
            data=FORM,
            headers={"X-Twilio-Signature": "not-a-real-signature"},
        )

        assert response.status_code == 403
        assert spy["calls"] == []

    def test_valid_signature_is_accepted(
        self, client, spy, monkeypatch, open_allowlist
    ):
        monkeypatch.setattr(config, "SKIP_SIGNATURE_VALIDATION", False)
        monkeypatch.setattr(config, "TWILIO_AUTH_TOKEN", "secret")
        monkeypatch.setattr(config, "BASE_URL", "https://example.com")
        set_parse_result(
            monkeypatch, to_number="+911234567890", task="ask hours"
        )

        signature = security.expected_signature(
            "https://example.com/webhook/whatsapp", FORM
        )

        response = client.post(
            "/webhook/whatsapp", data=FORM, headers={"X-Twilio-Signature": signature}
        )

        assert response.status_code == 200
        assert spy["calls"] == ["+911234567890"]


class TestAllowlistGate:
    def test_blocked_sender_places_no_call_and_gets_no_reply(
        self, client, spy, monkeypatch, allow_signatures
    ):
        monkeypatch.setattr(config, "ALLOWED_WHATSAPP_NUMBERS", {"+919999999999"})

        response = client.post("/webhook/whatsapp", data=FORM)

        assert response.json() == {"status": "ignored"}
        assert spy["calls"] == []
        assert spy["messages"] == []


class TestHappyPath:
    def test_places_call_and_confirms_to_user(
        self, client, spy, monkeypatch, allow_signatures, open_allowlist
    ):
        set_parse_result(
            monkeypatch,
            to_number="+911234567890",
            task="ask if they are open sunday",
            callee_name="Joes Pizza",
        )

        response = client.post("/webhook/whatsapp", data=FORM)

        assert response.json()["status"] == "calling"
        assert spy["calls"] == ["+911234567890"]

        to, body = spy["messages"][0]
        assert to == "whatsapp:+918837557003"
        assert body.startswith("📞 Calling Joes Pizza")

    def test_record_is_registered_under_call_sid(
        self, client, spy, monkeypatch, allow_signatures, open_allowlist
    ):
        set_parse_result(monkeypatch, to_number="+911234567890", task="ask hours")

        client.post("/webhook/whatsapp", data=FORM)

        record = main.registry.get("CA_fake")
        assert record is not None
        assert record.user_wa_number == "whatsapp:+918837557003"
        assert record.task == "ask hours"


class TestFailurePaths:
    def test_clarification_asks_user_and_places_no_call(
        self, client, spy, monkeypatch, allow_signatures, open_allowlist
    ):
        set_parse_result(monkeypatch, clarification_needed="Which number should I call?")

        response = client.post("/webhook/whatsapp", data=FORM)

        assert response.json()["status"] == "clarification_requested"
        assert spy["calls"] == []
        assert spy["messages"][0][1] == "Which number should I call?"

    def test_twilio_rejection_notifies_user(
        self, client, spy, monkeypatch, allow_signatures, open_allowlist
    ):
        set_parse_result(monkeypatch, to_number="+911234567890", task="ask hours")

        async def rejecting_call(to_number):
            raise main.telephony.TelephonyError("unverified number")

        monkeypatch.setattr(main.telephony, "place_call", rejecting_call)

        response = client.post("/webhook/whatsapp", data=FORM)

        assert response.json()["status"] == "call_failed"
        assert "❌" in spy["messages"][0][1]
        assert "unverified number" in spy["messages"][0][1]


class TestVoiceEndpoint:
    def test_twiml_points_at_the_websocket(self, client, monkeypatch):
        monkeypatch.setattr(config, "WS_BASE_URL", "wss://example.com")

        response = client.post("/voice")

        assert response.headers["content-type"].startswith("application/xml")
        assert "<Connect>" in response.text
        assert 'wss://example.com/media-stream' in response.text
