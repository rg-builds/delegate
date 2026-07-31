from fastapi import FastAPI, Request
from fastapi.responses import Response
from fastapi import WebSocket
from dotenv import load_dotenv
import os
import re
import httpx
load_dotenv()
import json
import audioop
from google.genai import types
from app.gemini import MODEL, client
from app import openai_realtime
import asyncio
import base64

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
NGROK_URL = os.getenv("NGROK_URL")

# Fallback number when the WhatsApp message doesn't include one
DEFAULT_TO_NUMBER = "+918837557003"

# Which realtime provider to use: "gemini" or "openai"
PROVIDER = os.getenv("PROVIDER", "gemini").lower()

url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Calls.json"

# call_sid -> task text. Populated when the call is placed, read when the
# media stream connects. In-memory only, fine for a single-process MVP.
pending_tasks: dict[str, str] = {}

app = FastAPI()


def parse_request(message: str) -> tuple[str, str]:
    """Split a WhatsApp message into (to_number, task).

    Looks for an E.164-ish number anywhere in the text. Whatever remains
    is treated as the task description.
    """
    match = re.search(r"\+?\d[\d\s\-]{8,}\d", message)

    if match:
        digits = re.sub(r"\D", "", match.group())
        if len(digits) == 10:            # bare Indian mobile number
            digits = "91" + digits
        to_number = "+" + digits
        task = (message[: match.start()] + message[match.end():]).strip()
    else:
        to_number = DEFAULT_TO_NUMBER
        task = message.strip()

    return to_number, task or "Have a brief, friendly conversation."


def build_system_instruction(task: str) -> str:
    return f"""You are Delegate, an AI assistant making a phone call on behalf of your user.

YOUR TASK FOR THIS CALL:
{task}

HOW TO BEHAVE ON THE CALL:
- Open with a short greeting, say you're calling on behalf of someone, and state why.
- Keep every response to one or two sentences. This is a phone call, not an essay.
- Speak naturally and conversationally. No markdown, no lists, no special characters.
- Ask one question at a time and wait for the answer.
- If the person seems confused, rephrase more simply instead of repeating yourself.
- Once the task is complete, thank them and close the conversation politely.

LANGUAGE:
- The person may speak English, Hindi, or mix both mid-sentence.
- Reply in whichever language they used most recently.
- Never comment on which language is being used.

AUDIO CONDITIONS:
- This is a phone line, so audio is low quality and words may be unclear.
- If you did not understand, ask them to repeat rather than guessing.
"""


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    form_data = await request.form()

    sender = form_data["From"]
    message = form_data["Body"]

    print(f"From: {sender}")
    print(f"Message: {message}")

    to_number, task = parse_request(message)
    print(f"Calling {to_number} with task: {task}")

    response = httpx.post(
        url=url,
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        data={
            "To": to_number,
            "From": TWILIO_PHONE_NUMBER,
            "Url": f"{NGROK_URL}/voice",
        },
    )

    if response.status_code >= 300:
        print(f"Twilio call failed: {response.status_code} {response.text}")
        return {"status": "call_failed"}

    call_sid = response.json()["sid"]
    pending_tasks[call_sid] = task
    print(f"Call queued: {call_sid}")

    return {"status": "calling", "call_sid": call_sid}


@app.post("/voice")
async def voice():
    print("VOICE ENDPOINT HIT")

    twiml = f"""
    <Response>
        <Connect>
            <Stream url="{NGROK_URL.replace('https', 'wss')}/media-stream"/>
        </Connect>
    </Response>
    """.strip()

    return Response(content=twiml, media_type="application/xml")


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()
    print("WEBSOCKET CONNECTED")

    # Wait for the 'start' event before opening Gemini, because the system
    # instruction has to be set at connect time and it depends on the task.
    stream_sid = None
    call_sid = None

    while stream_sid is None:
        data = json.loads(await websocket.receive_text())
        if data["event"] == "start":
            stream_sid = data["start"]["streamSid"]
            call_sid = data["start"].get("callSid")
            print(f"Call started, streamSid: {stream_sid}, callSid: {call_sid}")
        elif data["event"] == "stop":
            print("Call ended before it started")
            return

    task = pending_tasks.pop(call_sid, "Have a brief, friendly conversation.")
    print(f"Task for this call: {task} (provider: {PROVIDER})")

    if PROVIDER == "openai":
        await openai_realtime.handle_media_stream(
            websocket, stream_sid, build_system_instruction(task)
        )
        return

    # Resampler states - must persist across chunks to avoid clicks at boundaries
    inbound_state = None    # 8kHz  -> 16kHz (caller -> Gemini)
    outbound_state = None   # 24kHz -> 8kHz  (Gemini -> caller)

    transcript = []          # committed turns
    user_buffer = []         # partial transcription fragments
    gemini_buffer = []

    def commit(role: str, buffer: list):
        """Join streamed transcript fragments into one entry."""
        if not buffer:
            return
        text = "".join(buffer).strip()
        buffer.clear()
        if text:
            transcript.append({"role": role, "text": text})
            print(f"[{role}] {text}")

    async with client.aio.live.connect(
        model=MODEL,
        config={
            "response_modalities": ["AUDIO"],
            "output_audio_transcription": {},
            "input_audio_transcription": {},
            "system_instruction": build_system_instruction(task),
        },
    ) as session:
        print("Connected to Gemini")

        # Nudge Gemini to speak first - it's an outbound call, so the AI opens.
        await session.send_client_content(
            turns=types.Content(
                role="user",
                parts=[types.Part(text="The call has just connected. Greet them and begin your task.")],
            )
        )

        # Task 1: Twilio → Gemini
        async def listen_twilio():
            nonlocal inbound_state
            try:
                while True:
                    data = json.loads(await websocket.receive_text())
                    event = data["event"]

                    if event == "media":
                        audio_bytes = base64.b64decode(data["media"]["payload"])
                        pcm_bytes = audioop.ulaw2lin(audio_bytes, 2)
                        pcm_16k, inbound_state = audioop.ratecv(
                            pcm_bytes, 2, 1, 8000, 16000, inbound_state
                        )
                        await session.send_realtime_input(
                            audio=types.Blob(data=pcm_16k, mime_type="audio/pcm;rate=16000")
                        )

                    elif event == "stop":
                        print("Call ended")
                        break
            except Exception as e:
                print(f"listen_twilio error: {e}")

        # Task 2: Gemini → Twilio
        async def listen_gemini():
            nonlocal outbound_state
            try:
                while True:
                    async for message in session.receive():
                        server_content = message.server_content
                        if not server_content:
                            continue

                        # Caller barged in: stop playback and flush Twilio's buffer
                        if server_content.interrupted:
                            print("INTERRUPTED - flushing Twilio buffer")
                            outbound_state = None
                            commit("assistant", gemini_buffer)
                            await websocket.send_text(json.dumps({
                                "event": "clear",
                                "streamSid": stream_sid,
                            }))

                        # Transcript fragments - buffer, don't commit yet
                        if server_content.input_transcription:
                            if server_content.input_transcription.text:
                                user_buffer.append(server_content.input_transcription.text)

                        if server_content.output_transcription:
                            if server_content.output_transcription.text:
                                gemini_buffer.append(server_content.output_transcription.text)

                        if server_content.model_turn:
                            for part in server_content.model_turn.parts:
                                if part.inline_data:
                                    audio_data = part.inline_data.data  # PCM 24kHz bytes

                                    # Resample 24kHz → 8kHz (state carried across chunks)
                                    pcm_8k, outbound_state = audioop.ratecv(
                                        audio_data, 2, 1, 24000, 8000, outbound_state
                                    )

                                    mulaw_bytes = audioop.lin2ulaw(pcm_8k, 2)
                                    payload = base64.b64encode(mulaw_bytes).decode("ascii")

                                    await websocket.send_text(json.dumps({
                                        "event": "media",
                                        "streamSid": stream_sid,
                                        "media": {"payload": payload},
                                    }))

                        # Turn finished - flush both buffers into the transcript
                        if server_content.turn_complete:
                            commit("user", user_buffer)
                            commit("assistant", gemini_buffer)
            except Exception as e:
                print(f"listen_gemini error: {e}")

        listen_twilio_task = asyncio.create_task(listen_twilio())
        listen_gemini_task = asyncio.create_task(listen_gemini())

        done, pending = await asyncio.wait(
            [listen_twilio_task, listen_gemini_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()

        commit("user", user_buffer)
        commit("assistant", gemini_buffer)

        print("--- CALL TRANSCRIPT ---")
        for entry in transcript:
            print(f"{entry['role']}: {entry['text']}")
