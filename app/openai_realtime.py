"""OpenAI Realtime API bridge for Twilio Media Streams.

Raw WebSocket, no SDK - same first-principles approach as the Twilio REST calls.

The big difference from the Gemini path: OpenAI Realtime speaks G.711 mu-law
at 8kHz natively (`audio/pcmu`), which is exactly what Twilio sends. So there
is no decode, no resample, no re-encode. Twilio's base64 goes straight through
in both directions.
"""

import asyncio
import base64
import json
import os

from fastapi import WebSocket
from websockets.asyncio.client import connect

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1")
OPENAI_VOICE = os.getenv("OPENAI_VOICE", "marin")

REALTIME_URL = f"wss://api.openai.com/v1/realtime?model={OPENAI_REALTIME_MODEL}"


def build_session_config(instructions: str) -> dict:
    """GA-shape session config. Audio settings are nested under session.audio."""
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": instructions,
            "audio": {
                "input": {
                    # audio/pcmu == G.711 mu-law 8kHz == what Twilio sends
                    "format": {"type": "audio/pcmu"},
                    "turn_detection": {"type": "server_vad"},
                    "transcription": {"model": "whisper-1"},
                },
                "output": {
                    "format": {"type": "audio/pcmu"},
                    "voice": OPENAI_VOICE,
                },
            },
        },
    }


async def handle_media_stream(websocket: WebSocket, stream_sid: str, instructions: str):
    """Bridge an already-started Twilio media stream to OpenAI Realtime.

    Assumes the caller has already consumed Twilio's `start` event and passes
    in the stream_sid, mirroring how the Gemini handler is structured.
    """
    if not OPENAI_API_KEY:
        print("OPENAI_API_KEY is not set")
        return

    transcript = []

    async with connect(
        REALTIME_URL,
        additional_headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
    ) as openai_ws:
        print("Connected to OpenAI Realtime")

        await openai_ws.send(json.dumps(build_session_config(instructions)))

        # Outbound call, so the assistant opens the conversation.
        await openai_ws.send(json.dumps({"type": "response.create"}))

        # Task 1: Twilio -> OpenAI (pass-through, no conversion)
        async def listen_twilio():
            try:
                while True:
                    data = json.loads(await websocket.receive_text())
                    event = data["event"]

                    if event == "media":
                        await openai_ws.send(json.dumps({
                            "type": "input_audio_buffer.append",
                            "audio": data["media"]["payload"],
                        }))

                    elif event == "stop":
                        print("Call ended")
                        break
            except Exception as e:
                print(f"listen_twilio error: {e}")

        # Task 2: OpenAI -> Twilio
        async def listen_openai():
            try:
                async for raw in openai_ws:
                    event = json.loads(raw)
                    etype = event.get("type")

                    # Audio out. GA renamed response.audio.delta ->
                    # response.output_audio.delta, so accept both.
                    if etype in ("response.output_audio.delta", "response.audio.delta"):
                        await websocket.send_text(json.dumps({
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {"payload": event["delta"]},
                        }))

                    # Caller started talking - flush whatever Twilio has queued
                    elif etype == "input_audio_buffer.speech_started":
                        print("INTERRUPTED - flushing Twilio buffer")
                        await websocket.send_text(json.dumps({
                            "event": "clear",
                            "streamSid": stream_sid,
                        }))

                    # What the caller said
                    elif etype == "conversation.item.input_audio_transcription.completed":
                        text = (event.get("transcript") or "").strip()
                        if text:
                            transcript.append({"role": "user", "text": text})
                            print(f"[user] {text}")

                    # What the assistant said - arrives complete, not fragmented
                    elif etype in (
                        "response.output_audio_transcript.done",
                        "response.audio_transcript.done",
                    ):
                        text = (event.get("transcript") or "").strip()
                        if text:
                            transcript.append({"role": "assistant", "text": text})
                            print(f"[assistant] {text}")

                    elif etype == "error":
                        print(f"OpenAI error: {event.get('error')}")
            except Exception as e:
                print(f"listen_openai error: {e}")

        twilio_task = asyncio.create_task(listen_twilio())
        openai_task = asyncio.create_task(listen_openai())

        _, pending = await asyncio.wait(
            [twilio_task, openai_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()

    print("--- CALL TRANSCRIPT ---")
    for entry in transcript:
        print(f"{entry['role']}: {entry['text']}")
