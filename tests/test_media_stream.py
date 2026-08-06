"""Integration tests for the media-stream endpoint, with no phone and no OpenAI.

A fake Twilio client replays start/media/stop frames against the real endpoint.
"""

import json

import pytest
from fastapi.testclient import TestClient

from app import main
from app.state import CallRecord, registry


@pytest.fixture
def client():
    return TestClient(main.app)


@pytest.fixture
def bridge_calls(monkeypatch):
    """Replace the realtime bridge so no OpenAI socket is opened."""
    calls = []

    async def fake_bridge(websocket, stream_sid, record):
        calls.append({"stream_sid": stream_sid, "record": record})
        record.add_turn("callee", "Yes we are open.")

    monkeypatch.setattr(main.realtime, "handle_media_stream", fake_bridge)
    return calls


@pytest.fixture
def reports(monkeypatch):
    sent = []

    async def fake_report(record):
        sent.append(record)

    monkeypatch.setattr(main.reporting, "report", fake_report)
    return sent


def start_frame(call_sid="CA_test", stream_sid="MZ_test"):
    return json.dumps({
        "event": "start",
        "start": {"streamSid": stream_sid, "callSid": call_sid},
    })


def test_bridges_known_call_then_reports(client, bridge_calls, reports):
    registry.add(CallRecord(
        call_sid="CA_known",
        user_wa_number="whatsapp:+918837557003",
        to_number="+911234567890",
        task="ask if they are open",
    ))

    with client.websocket_connect("/media-stream") as ws:
        ws.send_text(start_frame(call_sid="CA_known", stream_sid="MZ_known"))

    assert len(bridge_calls) == 1
    assert bridge_calls[0]["stream_sid"] == "MZ_known"
    assert bridge_calls[0]["record"].task == "ask if they are open"

    assert len(reports) == 1
    assert reports[0].call_sid == "CA_known"


def test_unknown_call_sid_is_dropped_without_reporting(client, bridge_calls, reports):
    with client.websocket_connect("/media-stream") as ws:
        ws.send_text(start_frame(call_sid="CA_never_registered"))

    assert bridge_calls == []
    assert reports == []


def test_stop_before_start_exits_cleanly(client, bridge_calls, reports):
    with client.websocket_connect("/media-stream") as ws:
        ws.send_text(json.dumps({"event": "stop"}))

    assert bridge_calls == []
    assert reports == []


def test_connected_frame_before_start_is_tolerated(client, bridge_calls, reports):
    registry.add(CallRecord(
        call_sid="CA_conn",
        user_wa_number="whatsapp:+918837557003",
        to_number="+911234567890",
        task="ask hours",
    ))

    with client.websocket_connect("/media-stream") as ws:
        ws.send_text(json.dumps({"event": "connected", "protocol": "Call"}))
        ws.send_text(start_frame(call_sid="CA_conn"))

    assert len(bridge_calls) == 1
    assert len(reports) == 1


def test_bridge_crash_still_reports(client, reports, monkeypatch):
    async def exploding_bridge(websocket, stream_sid, record):
        raise RuntimeError("openai socket died")

    monkeypatch.setattr(main.realtime, "handle_media_stream", exploding_bridge)

    registry.add(CallRecord(
        call_sid="CA_crash",
        user_wa_number="whatsapp:+918837557003",
        to_number="+911234567890",
        task="ask hours",
    ))

    with client.websocket_connect("/media-stream") as ws:
        ws.send_text(start_frame(call_sid="CA_crash"))

    assert len(reports) == 1
    assert reports[0].status == "failed"


def test_record_marked_in_progress_during_bridge(client, bridge_calls, reports):
    registry.add(CallRecord(
        call_sid="CA_status",
        user_wa_number="whatsapp:+918837557003",
        to_number="+911234567890",
        task="ask hours",
    ))

    with client.websocket_connect("/media-stream") as ws:
        ws.send_text(start_frame(call_sid="CA_status"))

    assert bridge_calls[0]["record"].status == "in-progress"
