"""Twilio webhook authentication.

Twilio signs every webhook: HMAC-SHA1 over the full URL with the POST params
appended in sorted order, keyed by the account auth token, base64 encoded.
Implemented directly rather than pulling in the Twilio SDK.
"""

import base64
import hashlib
import hmac

from fastapi import Request

from app import config


def expected_signature(url: str, params: dict[str, str]) -> str:
    payload = url + "".join(f"{key}{params[key]}" for key in sorted(params))

    digest = hmac.new(
        (config.TWILIO_AUTH_TOKEN or "").encode(),
        payload.encode("utf-8"),
        hashlib.sha1,
    ).digest()

    return base64.b64encode(digest).decode()


def public_url(request: Request) -> str:
    """The URL Twilio signed, which is the public one, not the local one."""
    return f"{config.BASE_URL}{request.url.path}"


async def is_valid_twilio_request(request: Request, params: dict[str, str]) -> bool:
    if config.SKIP_SIGNATURE_VALIDATION:
        return True

    signature = request.headers.get("X-Twilio-Signature")
    if not signature:
        return False

    return hmac.compare_digest(expected_signature(public_url(request), params), signature)


def is_allowed_sender(from_number: str) -> bool:
    """Allowlist check. An empty allowlist permits everyone (dev convenience)."""
    if not config.ALLOWED_WHATSAPP_NUMBERS:
        return True

    return from_number.replace("whatsapp:", "") in config.ALLOWED_WHATSAPP_NUMBERS
