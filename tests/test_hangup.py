import asyncio
import json

import pytest

from app import realtime
from app.prompts import build_system_instruction


class FakeTwilioSocket:
    def __init__(self):
        self.sent = []

    async def send_text(self, text):
        self.sent.append(json.loads(text))


class TestWaitForPlayback:
    @pytest.mark.asyncio
    async def test_sends_a_mark_to_twilio(self):
        ws = FakeTwilioSocket()
        played = asyncio.Event()
        played.set()

        await realtime._wait_for_playback(ws, "MZ1", played)

        assert ws.sent == [{
            "event": "mark",
            "streamSid": "MZ1",
            "mark": {"name": realtime.GOODBYE_MARK},
        }]

    @pytest.mark.asyncio
    async def test_waits_until_twilio_confirms_playback(self):
        ws = FakeTwilioSocket()
        played = asyncio.Event()

        async def confirm_later():
            await asyncio.sleep(0.05)
            played.set()

        asyncio.create_task(confirm_later())

        # Would raise/timeout if it did not actually wait
        await realtime._wait_for_playback(ws, "MZ1", played)
        assert played.is_set()

    @pytest.mark.asyncio
    async def test_gives_up_rather_than_hanging_forever(self, monkeypatch):
        """If Twilio never echoes the mark, we must still hang up."""
        monkeypatch.setattr(realtime, "PLAYBACK_DRAIN_TIMEOUT", 0.05)

        ws = FakeTwilioSocket()
        never_played = asyncio.Event()

        await realtime._wait_for_playback(ws, "MZ1", never_played)

        assert not never_played.is_set()


class TestGoodbyeInstructions:
    def test_agent_is_told_to_recap_before_ending(self):
        instructions = build_system_instruction("ask what time they open").lower()
        assert "recap" in instructions

    def test_agent_is_told_not_to_end_abruptly(self):
        instructions = build_system_instruction("ask what time they open").lower()
        assert "never hang up mid-thought" in instructions
        assert "bare 'ok' or 'bye'" in instructions

    def test_goodbye_comes_before_end_call(self):
        instructions = build_system_instruction("ask what time they open").lower()
        assert "only once you have said that closing line" in instructions

    def test_agent_wraps_up_if_caller_wants_to_go(self):
        instructions = build_system_instruction("ask what time they open").lower()
        assert "clearly want to go" in instructions
