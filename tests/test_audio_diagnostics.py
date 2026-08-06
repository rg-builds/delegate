import base64

from app.realtime import _is_silent


def encode(byte_values: list[int]) -> str:
    return base64.b64encode(bytes(byte_values)).decode()


class TestSilenceDetection:
    def test_pure_mulaw_silence_is_detected(self):
        assert _is_silent(encode([0xFF] * 160))

    def test_alternate_silence_byte_is_detected(self):
        assert _is_silent(encode([0x7F] * 160))

    def test_real_speech_is_not_silent(self):
        # Varied amplitudes, as speech produces
        assert not _is_silent(encode([0x10, 0x40, 0x80, 0x22, 0x55] * 32))

    def test_mostly_silent_with_a_little_noise_still_counts_as_silent(self):
        frame = [0xFF] * 155 + [0x20] * 5
        assert _is_silent(encode(frame))

    def test_half_speech_is_not_silent(self):
        frame = [0xFF] * 80 + [0x20] * 80
        assert not _is_silent(encode(frame))

    def test_empty_payload_is_silent(self):
        assert _is_silent("")

    def test_malformed_base64_does_not_raise(self):
        assert _is_silent("!!!not base64!!!") is False


class TestNoiseReductionToggle:
    def test_off_sends_null_so_the_api_leaves_audio_untouched(self, monkeypatch):
        from app import config
        from app.realtime import build_session_config
        from app.state import CallRecord

        monkeypatch.setattr(config, "NOISE_REDUCTION", "off")

        record = CallRecord(
            call_sid="CA1",
            user_wa_number="whatsapp:+910000000000",
            to_number="+911234567890",
            task="ask hours",
        )
        audio = build_session_config(record)["session"]["audio"]

        assert audio["input"]["noise_reduction"] is None

    def test_named_mode_is_passed_through(self, monkeypatch):
        from app import config
        from app.realtime import build_session_config
        from app.state import CallRecord

        monkeypatch.setattr(config, "NOISE_REDUCTION", "near_field")

        record = CallRecord(
            call_sid="CA1",
            user_wa_number="whatsapp:+910000000000",
            to_number="+911234567890",
            task="ask hours",
        )
        audio = build_session_config(record)["session"]["audio"]

        assert audio["input"]["noise_reduction"] == {"type": "near_field"}
