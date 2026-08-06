"""Twilio REST helpers. Raw HTTP, no SDK."""

import httpx

from app import config

AUTH = (config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)

# Twilio call statuses that mean "no conversation happened"
DEAD_STATUSES = {"busy", "no-answer", "failed", "canceled"}


class TelephonyError(Exception):
    pass


async def place_call(to_number: str) -> str:
    """Start an outbound call. Returns the Call SID."""
    data = {
        "To": to_number,
        "From": config.TWILIO_PHONE_NUMBER,
        "Url": f"{config.BASE_URL}/voice",
        "StatusCallback": f"{config.BASE_URL}/webhook/call-status",
        "StatusCallbackMethod": "POST",
        "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"],
    }

    async with httpx.AsyncClient(timeout=15) as http:
        response = await http.post(config.TWILIO_CALLS_URL, auth=AUTH, data=data)

    if response.status_code >= 300:
        raise TelephonyError(_twilio_message(response))

    return response.json()["sid"]


async def hangup_call(call_sid: str) -> None:
    """End an in-progress call."""
    url = f"{config.TWILIO_API_ROOT}/Calls/{call_sid}.json"

    async with httpx.AsyncClient(timeout=15) as http:
        response = await http.post(url, auth=AUTH, data={"Status": "completed"})

    if response.status_code >= 300:
        print(f"hangup_call failed: {response.status_code} {_twilio_message(response)}")


async def send_whatsapp(to_number: str, body: str) -> None:
    """Send a WhatsApp message. `to_number` may include the whatsapp: prefix."""
    to = to_number if to_number.startswith("whatsapp:") else f"whatsapp:{to_number}"

    data = {
        "From": f"whatsapp:{config.TWILIO_WHATSAPP_NUMBER}",
        "To": to,
        "Body": body[:1500],
    }

    async with httpx.AsyncClient(timeout=15) as http:
        response = await http.post(config.TWILIO_MESSAGES_URL, auth=AUTH, data=data)

    if response.status_code >= 300:
        print(f"send_whatsapp failed: {response.status_code} {_twilio_message(response)}")


def _twilio_message(response: httpx.Response) -> str:
    try:
        return response.json().get("message", response.text)
    except Exception:
        return response.text
