from app import config
from app.prompts import build_system_instruction
from app.realtime import build_session_config, build_transcription, build_turn_detection
from app.state import CallRecord


def make_record(**overrides):
    defaults = dict(
        call_sid="CA1",
        user_wa_number="whatsapp:+910000000000",
        to_number="+911234567890",
        task="remind him about his gym membership",
    )
    defaults.update(overrides)
    return CallRecord(**defaults)


class TestTranscriptionConfig:
    def test_does_not_use_whisper_1(self):
        """whisper-1 is measurably worse on 8kHz Hinglish."""
        assert build_transcription(make_record())["model"] != "whisper-1"

    def test_model_is_configurable(self, monkeypatch):
        monkeypatch.setattr(config, "OPENAI_TRANSCRIBE_MODEL", "gpt-4o-mini-transcribe")
        assert build_transcription(make_record())["model"] == "gpt-4o-mini-transcribe"

    def test_prompt_biases_toward_hinglish_and_digits(self):
        prompt = build_transcription(make_record())["prompt"]

        assert "Hinglish" in prompt
        assert "baje" in prompt
        assert "digits" in prompt

    def test_prompt_includes_task_for_domain_vocabulary(self):
        prompt = build_transcription(make_record(task="ask about kettlebell classes"))
        assert "kettlebell" in prompt["prompt"]

    def test_prompt_includes_callee_name_when_known(self):
        prompt = build_transcription(make_record(callee_name="Albert"))["prompt"]
        assert "Albert" in prompt

    def test_prompt_is_length_capped(self):
        prompt = build_transcription(make_record(task="x" * 5000))["prompt"]
        assert len(prompt) <= 900


class TestTurnDetection:
    def test_prefix_padding_guards_against_clipped_first_syllable(self):
        """Low padding is how '6 baje' becomes just 'baje'."""
        assert build_turn_detection()["prefix_padding_ms"] >= 400

    def test_padding_is_configurable(self, monkeypatch):
        monkeypatch.setattr(config, "VAD_PREFIX_PADDING_MS", 800)
        assert build_turn_detection()["prefix_padding_ms"] == 800


class TestReadbackInstructions:
    def test_agent_is_told_to_confirm_numbers(self):
        instructions = build_system_instruction("ask what time they open")
        lowered = instructions.lower()

        assert "repeat it back" in lowered
        assert "confirm" in lowered

    def test_agent_is_forbidden_from_using_its_own_suggestion(self):
        instructions = build_system_instruction("ask what time they open").lower()
        assert "never fill in an option you suggested yourself" in instructions

    def test_agent_must_not_invent_values(self):
        instructions = build_system_instruction("ask what time they open").lower()
        assert "never assume a value" in instructions

    def test_end_call_only_records_confirmed_details(self):
        instructions = build_system_instruction("ask what time they open").lower()
        assert "only include details they actually confirmed" in instructions

    def test_readback_reaches_the_live_session_config(self):
        session = build_session_config(make_record())["session"]
        assert "repeat it back" in session["instructions"].lower()
