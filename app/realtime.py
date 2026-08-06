"""Twilio Media Streams <-> OpenAI Realtime bridge. Raw WebSocket, no SDK.

OpenAI Realtime speaks G.711 mu-law 8kHz natively (`audio/pcmu`), which is
exactly what Twilio sends. Audio passes through untouched in both directions -
no decoding, no resampling, no re-encoding.
"""

import asyncio
import base64
import json

from fastapi import WebSocket
from websockets.asyncio.client import connect

from app import config, telephony
from app.prompts import build_system_instruction
from app.state import CallRecord

END_CALL_TOOL = {
    "type": "function",
    "name": "end_call",
    "description": (
        "Call this when the task is complete, or when it clearly cannot be completed. "
        "After calling this, say one short goodbye sentence."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "outcome": {
                "type": "string",
                "enum": ["success", "failed", "declined"],
            },
            "summary": {
                "type": "string",
                "description": (
                    "One or two sentences: what was accomplished or why it failed, "
                    "including concrete details like times, prices and confirmation numbers."
                ),
            },
        },
        "required": ["outcome", "summary"],
    },
}

# Audio-out events. GA renamed response.audio.delta -> response.output_audio.delta.
AUDIO_DELTA_EVENTS = {"response.output_audio.delta", "response.audio.delta"}
ASSISTANT_TRANSCRIPT_DONE = {
    "response.output_audio_transcript.done",
    "response.audio_transcript.done",
}

# High-frequency events, not worth printing even in debug mode.
_QUIET_EVENTS = {
    "response.output_audio_transcript.delta",
    "response.audio_transcript.delta",
    "response.function_call_arguments.delta",
    "response.output_text.delta",
    "response.text.delta",
}

# Name of the Twilio mark used to detect that the goodbye finished playing.
GOODBYE_MARK = "goodbye"

# Safety net in case Twilio never echoes the mark back (e.g. caller already gone).
PLAYBACK_DRAIN_TIMEOUT = 15

# mu-law encodes silence as 0xFF / 0x7F. If nearly every byte is one of those,
# the frame carries no real audio.
_MULAW_SILENCE = {0xFF, 0x7F}


def _is_silent(payload_b64: str, threshold: float = 0.95) -> bool:
    try:
        raw = base64.b64decode(payload_b64)
    except Exception:
        return False

    if not raw:
        return True

    quiet = sum(1 for byte in raw if byte in _MULAW_SILENCE)
    return quiet / len(raw) >= threshold


def build_turn_detection() -> dict:
    if config.VAD_MODE == "semantic":
        return {"type": "semantic_vad", "eagerness": config.VAD_EAGERNESS}

    return {
        "type": "server_vad",
        "threshold": config.VAD_THRESHOLD,
        # Generous padding so the first syllable of a short reply isn't clipped.
        "prefix_padding_ms": config.VAD_PREFIX_PADDING_MS,
        "silence_duration_ms": config.VAD_SILENCE_MS,
    }


def build_transcription(record: CallRecord) -> dict:
    """Transcription config, biased toward this call's vocabulary.

    The prompt is a recognition hint, not an instruction. Feeding it the task
    makes domain words and Hinglish digits far more likely to be heard right.
    """
    hint_parts = [
        "Phone call in Hinglish: Hindi and English mixed freely.",
        "Times are said like '6 baje', '3 baje', 'subah', 'shaam', 'afternoon'.",
        "Transcribe digits as digits.",
    ]

    if record.callee_name:
        hint_parts.append(f"The person is called {record.callee_name}.")

    hint_parts.append(f"Call purpose: {record.task}")

    return {
        "model": config.OPENAI_TRANSCRIBE_MODEL,
        "prompt": " ".join(hint_parts)[:900],
    }


def build_session_config(record: CallRecord) -> dict:
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": build_system_instruction(
                record.task, record.callee_name, record.context
            ),
            "tools": [END_CALL_TOOL],
            "tool_choice": "auto",
            "audio": {
                "input": {
                    "format": {"type": "audio/pcmu"},
                    # Off by default: it can over-attenuate quiet phone audio to
                    # the point the model stops hearing the caller at all.
                    "noise_reduction": (
                        None if config.NOISE_REDUCTION == "off"
                        else {"type": config.NOISE_REDUCTION}
                    ),
                    "turn_detection": build_turn_detection(),
                    "transcription": build_transcription(record),
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": config.OPENAI_VOICE,
                },
            },
        },
    }


async def handle_media_stream(websocket: WebSocket, stream_sid: str, record: CallRecord):
    """Bridge an already-started Twilio media stream to OpenAI Realtime.

    Returns when the call is over, for any reason. Reporting is the caller's job.
    """
    try:
        await asyncio.wait_for(
            _bridge(websocket, stream_sid, record),
            timeout=config.MAX_CALL_SECONDS,
        )
    except asyncio.TimeoutError:
        print(f"Call {record.call_sid} hit the {config.MAX_CALL_SECONDS}s limit")
        record.status = "timed-out"
        await telephony.hangup_call(record.call_sid)


async def _bridge(websocket: WebSocket, stream_sid: str, record: CallRecord):
    async with connect(
        config.OPENAI_REALTIME_URL,
        additional_headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
    ) as openai_ws:
        print("Connected to OpenAI Realtime")

        await openai_ws.send(json.dumps(build_session_config(record)))

        # Outbound call, so the assistant opens the conversation.
        await openai_ws.send(json.dumps({"type": "response.create"}))

        # Set once end_call fires, so we hang up after the goodbye finishes.
        hangup_after_response = asyncio.Event()

        # Whether the agent is mid-response. Only then is there queued audio
        # worth discarding, so this stops stray VAD triggers cutting it off.
        speaking = False

        # Twilio echoes a mark back once it has finished PLAYING everything sent
        # before it. response.done only means OpenAI finished GENERATING, and
        # generation outruns realtime playback - so without this the goodbye
        # gets cut off mid-word.
        goodbye_played = asyncio.Event()

        async def pump_twilio_to_openai():
            """Twilio -> OpenAI. Pass-through, no conversion."""
            frames = 0
            silent_frames = 0
            try:
                while True:
                    data = json.loads(await websocket.receive_text())
                    event = data["event"]

                    if event == "media":
                        # Twilio only streams the inbound track for <Connect>,
                        # but guard anyway: feeding our own audio back would make
                        # the model respond to itself.
                        if data["media"].get("track") not in (None, "inbound"):
                            continue

                        payload = data["media"]["payload"]
                        frames += 1

                        if config.DEBUG_AUDIO:
                            if _is_silent(payload):
                                silent_frames += 1
                            if frames % 100 == 0:
                                print(
                                    f"[audio] {frames} frames from caller, "
                                    f"{silent_frames} near-silent"
                                )

                        await openai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": payload,
                        }))

                    elif event == "mark":
                        if data.get("mark", {}).get("name") == GOODBYE_MARK:
                            goodbye_played.set()

                    elif event == "stop":
                        print(f"Callee hung up after {frames} audio frames")
                        break
            except Exception as e:
                print(f"twilio pump stopped after {frames} frames: {e}")

        async def pump_openai_to_twilio():
            """OpenAI -> Twilio, plus transcripts and tool calls."""
            nonlocal speaking
            try:
                async for raw in openai_ws:
                    event = json.loads(raw)
                    etype = event.get("type")

                    if etype in AUDIO_DELTA_EVENTS:
                        await websocket.send_text(json.dumps({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": event["delta"]},
                        }))

                    elif etype == "response.created":
                        speaking = True

                    elif etype == "input_audio_buffer.speech_stopped":
                        if config.DEBUG_AUDIO:
                            print("[vad] speech_stopped - buffer will be committed")

                    elif etype == "input_audio_buffer.committed":
                        if config.DEBUG_AUDIO:
                            print("[vad] buffer committed to the model")

                    elif etype == "conversation.item.input_audio_transcription.failed":
                        print(f"[stt] transcription FAILED: {event.get('error')}")

                    elif etype == "input_audio_buffer.speech_started":
                        if config.DEBUG_AUDIO:
                            print("[vad] speech_started - caller is talking")
                        # Only a genuine barge-in has audio worth discarding.
                        # OpenAI cancels its own response (interrupt_response),
                        # so we just flush what Twilio has already buffered.
                        if speaking:
                            print("Barge-in - flushing queued audio")
                            speaking = False
                            await websocket.send_text(json.dumps({
                                "event": "clear",
                                "streamSid": stream_sid,
                            }))

                    elif etype == "conversation.item.input_audio_transcription.completed":
                        text = (event.get("transcript") or "").strip()
                        if text:
                            record.add_turn("callee", text)
                            print(f"[callee] {text}")

                    elif etype in ASSISTANT_TRANSCRIPT_DONE:
                        text = (event.get("transcript") or "").strip()
                        if text:
                            record.add_turn("delegate", text)
                            print(f"[delegate] {text}")

                    elif etype == "response.function_call_arguments.done":
                        await _handle_tool_call(
                            openai_ws, record, event, hangup_after_response
                        )

                    elif etype == "response.done":
                        speaking = False
                        if hangup_after_response.is_set():
                            await _wait_for_playback(websocket, stream_sid, goodbye_played)
                            await telephony.hangup_call(record.call_sid)
                            break

                    elif etype == "error":
                        print(f"OpenAI error: {event.get('error')}")

                    elif config.DEBUG_AUDIO and etype not in _QUIET_EVENTS:
                        print(f"[event] {etype}")
            except Exception as e:
                print(f"openai pump stopped: {e}")

        twilio_task = asyncio.create_task(pump_twilio_to_openai())
        openai_task = asyncio.create_task(pump_openai_to_twilio())

        _, pending = await asyncio.wait(
            [twilio_task, openai_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()


async def _wait_for_playback(websocket: WebSocket, stream_sid: str, played: asyncio.Event):
    """Block until Twilio has finished playing the audio queued so far.

    Sends a mark, which Twilio echoes back once everything before it has been
    played out to the caller. Without this we'd hang up while the goodbye is
    still sitting in Twilio's buffer.
    """
    await websocket.send_text(json.dumps({
        "event": "mark",
        "streamSid": stream_sid,
        "mark": {"name": GOODBYE_MARK},
    }))

    try:
        await asyncio.wait_for(played.wait(), timeout=PLAYBACK_DRAIN_TIMEOUT)
    except asyncio.TimeoutError:
        print("Timed out waiting for goodbye playback, hanging up anyway")


async def _handle_tool_call(openai_ws, record: CallRecord, event: dict, hangup_flag: asyncio.Event):
    """Record the end_call outcome, then let the model speak its goodbye."""
    if event.get("name") != "end_call":
        return

    try:
        args = json.loads(event.get("arguments") or "{}")
    except json.JSONDecodeError:
        args = {}

    record.outcome = args.get("outcome")
    record.tool_summary = args.get("summary")
    print(f"end_call: {record.outcome} - {record.tool_summary}")

    # Acknowledge the tool call so the model can produce its closing line.
    await openai_ws.send(json.dumps({
        "type": "conversation.item.create",
        "item": {
            "type": "function_call_output",
            "call_id": event.get("call_id"),
            "output": json.dumps({"acknowledged": True}),
        },
    }))
    await openai_ws.send(json.dumps({"type": "response.create"}))

    hangup_flag.set()
