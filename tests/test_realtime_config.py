from app import config
from app.realtime import END_CALL_TOOL, build_session_config, build_turn_detection
from app.state import CallRecord


def make_record(**overrides):
    defaults = dict(
        call_sid="CA1",
        user_wa_number="whatsapp:+910000000000",
        to_number="+911234567890",
        task="ask if they are open sunday",
    )
    defaults.update(overrides)
    return CallRecord(**defaults)


class TestTurnDetection:
    def test_server_vad_is_less_sensitive_than_api_defaults(self, monkeypatch):
        """API defaults (0.5 / 500ms) cut the agent off on phone noise."""
        monkeypatch.setattr(config, "VAD_MODE", "server")
        monkeypatch.setattr(config, "VAD_THRESHOLD", 0.65)
        monkeypatch.setattr(config, "VAD_SILENCE_MS", 700)

        vad = build_turn_detection()

        assert vad["type"] == "server_vad"
        assert vad["threshold"] > 0.5
        assert vad["silence_duration_ms"] > 500

    def test_semantic_mode_switches_type(self, monkeypatch):
        monkeypatch.setattr(config, "VAD_MODE", "semantic")
        monkeypatch.setattr(config, "VAD_EAGERNESS", "low")

        vad = build_turn_detection()

        assert vad == {"type": "semantic_vad", "eagerness": "low"}


class TestSessionConfig:
    def test_both_directions_use_pcmu_so_no_conversion_is_needed(self):
        audio = build_session_config(make_record())["session"]["audio"]

        assert audio["input"]["format"] == {"type": "audio/pcmu"}
        assert audio["output"]["format"] == {"type": "audio/pcmu"}

    def test_noise_reduction_is_configurable(self, monkeypatch):
        monkeypatch.setattr(config, "NOISE_REDUCTION", "far_field")
        audio = build_session_config(make_record())["session"]["audio"]
        assert audio["input"]["noise_reduction"] == {"type": "far_field"}

    def test_end_call_tool_is_exposed(self):
        session = build_session_config(make_record())["session"]

        assert session["tools"] == [END_CALL_TOOL]
        assert session["tool_choice"] == "auto"

    def test_end_call_requires_outcome_and_summary(self):
        params = END_CALL_TOOL["parameters"]

        assert set(params["required"]) == {"outcome", "summary"}
        assert params["properties"]["outcome"]["enum"] == ["success", "failed", "declined"]

    def test_task_and_callee_reach_the_instructions(self):
        record = make_record(callee_name="Joes Pizza", context="party of 2")
        instructions = build_session_config(record)["session"]["instructions"]

        assert "ask if they are open sunday" in instructions
        assert "Joes Pizza" in instructions
        assert "party of 2" in instructions

    def test_instructions_tell_the_agent_to_call_end_call(self):
        instructions = build_session_config(make_record())["session"]["instructions"]
        assert "end_call" in instructions
