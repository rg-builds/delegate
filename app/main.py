"""Delegate - AI representative that makes phone calls on your behalf.

Routes only. Business logic lives in the modules this imports.

    WhatsApp -> /webhook/whatsapp -> Twilio REST -> phone call
                                                        |
    caller <-> Twilio Media Streams <-> /media-stream <-> OpenAI Realtime
                                                        |
                                    /webhook/call-status -> WhatsApp result
"""

import json
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import Response

from app import config, parsing, realtime, reporting, security, telephony
from app.state import CallRecord, registry


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Fail fast on missing configuration rather than mid-call."""
    missing = config.missing_required()
    if missing:
        raise RuntimeError(f"Missing required settings: {', '.join(missing)}")
    print(f"Delegate ready. Base URL: {config.BASE_URL}")
    yield


app = FastAPI(title="Delegate", lifespan=lifespan)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/webhook/whatsapp")
async def whatsapp_webhook(request: Request):
    form = dict(await request.form())

    if not await security.is_valid_twilio_request(request, form):
        return Response(status_code=403)

    sender = form.get("From", "")
    message = form.get("Body", "")

    if not security.is_allowed_sender(sender):
        print(f"Ignoring message from non-allowlisted sender: {sender}")
        return {"status": "ignored"}

    print(f"[{sender}] {message}")

    request_details = await parsing.parse(message)

    if not request_details.is_actionable:
        await telephony.send_whatsapp(sender, request_details.clarification_needed)
        return {"status": "clarification_requested"}

    try:
        call_sid = await telephony.place_call(request_details.to_number)
    except telephony.TelephonyError as e:
        await telephony.send_whatsapp(sender, f"❌ Couldn't place the call: {e}")
        return {"status": "call_failed"}

    registry.add(CallRecord(
        call_sid=call_sid,
        user_wa_number=sender,
        to_number=request_details.to_number,
        task=request_details.task,
        callee_name=request_details.callee_name,
        context=request_details.context,
    ))

    who = request_details.callee_name or request_details.to_number
    await telephony.send_whatsapp(sender, f"📞 Calling {who}: {request_details.task}")

    return {"status": "calling", "call_sid": call_sid}


@app.post("/voice")
async def voice():
    """TwiML telling Twilio to stream the call audio to our WebSocket."""
    twiml = (
        "<Response>"
        "<Connect>"
        f'<Stream url="{config.WS_BASE_URL}/media-stream"/>'
        "</Connect>"
        "</Response>"
    )
    return Response(content=twiml, media_type="application/xml")


@app.post("/webhook/call-status")
async def call_status_webhook(request: Request):
    form = dict(await request.form())

    if not await security.is_valid_twilio_request(request, form):
        return Response(status_code=403)

    call_sid = form.get("CallSid")
    status = form.get("CallStatus")
    print(f"Call {call_sid} -> {status}")

    record = registry.get(call_sid)
    if not record:
        return {"status": "unknown_call"}

    if status == "in-progress":
        record.status = "in-progress"
    elif status == "ringing":
        record.status = "ringing"
    elif status in telephony.DEAD_STATUSES:
        record.status = status
        await reporting.report(record)
    elif status == "completed":
        # Normal end. The media stream handler usually reports first; this is
        # the backstop for when it doesn't (e.g. the bridge never opened).
        await reporting.report(record)

    return {"status": "ok"}


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket):
    await websocket.accept()

    # Wait for `start`, which carries the Call SID we correlate everything by.
    stream_sid = None
    call_sid = None

    while stream_sid is None:
        data = json.loads(await websocket.receive_text())
        if data["event"] == "start":
            stream_sid = data["start"]["streamSid"]
            call_sid = data["start"].get("callSid")
        elif data["event"] == "stop":
            return

    record = registry.get(call_sid)
    if not record:
        print(f"No record for callSid {call_sid}, dropping stream")
        return

    record.status = "in-progress"
    print(f"Bridging call {call_sid}: {record.task}")

    try:
        await realtime.handle_media_stream(websocket, stream_sid, record)
    except Exception as e:
        print(f"Bridge failed: {e}")
        if not record.outcome:
            record.status = "failed"

    await reporting.report(record)
