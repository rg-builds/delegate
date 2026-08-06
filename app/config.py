"""Central configuration. Every module reads env vars from here."""

import os

from dotenv import load_dotenv

load_dotenv()

# --- Twilio ---
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_PHONE_NUMBER = os.getenv("TWILIO_PHONE_NUMBER")
TWILIO_WHATSAPP_NUMBER = (
    os.getenv("TWILIO_WHATSAPP_NUMBER")
    or os.getenv("TWILIO_WHATSAPP_FROM")      # older name, kept working
    or "+14155238886"                          # Twilio sandbox default
).replace("whatsapp:", "")

TWILIO_API_ROOT = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}"
TWILIO_CALLS_URL = f"{TWILIO_API_ROOT}/Calls.json"
TWILIO_MESSAGES_URL = f"{TWILIO_API_ROOT}/Messages.json"

# --- OpenAI ---
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_REALTIME_MODEL = os.getenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1")
OPENAI_TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-5-mini")
OPENAI_VOICE = os.getenv("OPENAI_VOICE", "marin")

# whisper-1 is noticeably worse than gpt-4o-transcribe on 8kHz phone audio.
OPENAI_TRANSCRIBE_MODEL = os.getenv("OPENAI_TRANSCRIBE_MODEL", "gpt-4o-transcribe")

OPENAI_REALTIME_URL = f"wss://api.openai.com/v1/realtime?model={OPENAI_REALTIME_MODEL}"
OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"

# --- Public base URL (ngrok in dev, real host in prod) ---
BASE_URL = (os.getenv("BASE_URL") or os.getenv("NGROK_URL") or "").rstrip("/")
WS_BASE_URL = BASE_URL.replace("https://", "wss://").replace("http://", "ws://")

# --- Behaviour ---
MAX_CALL_SECONDS = int(os.getenv("MAX_CALL_SECONDS", "300"))

# --- Voice activity detection ---
# Defaults match the API's own (0.5 / 500ms). Raising the threshold makes the
# agent less likely to cut itself off, but too high and quiet phone audio never
# registers as speech at all, so the model answers having heard nothing.
VAD_MODE = os.getenv("VAD_MODE", "server")            # "server" | "semantic"
VAD_THRESHOLD = float(os.getenv("VAD_THRESHOLD", "0.4"))
VAD_SILENCE_MS = int(os.getenv("VAD_SILENCE_MS", "600"))
VAD_EAGERNESS = os.getenv("VAD_EAGERNESS", "medium")  # semantic mode only
# How much audio before the detected speech onset is kept. Too low and the first
# syllable is clipped, which is how "6 baje" gets heard as just "baje".
VAD_PREFIX_PADDING_MS = int(os.getenv("VAD_PREFIX_PADDING_MS", "500"))
# "off" disables it. Noise reduction can over-attenuate 8kHz phone audio.
NOISE_REDUCTION = os.getenv("NOISE_REDUCTION", "off")  # off | near_field | far_field

# Logs every realtime event and audio counters, to diagnose listening problems.
DEBUG_AUDIO = os.getenv("DEBUG_AUDIO") == "1"

# --- Security ---
# Comma-separated E.164 numbers allowed to trigger calls.
ALLOWED_WHATSAPP_NUMBERS = {
    n.strip().replace("whatsapp:", "")
    for n in (os.getenv("ALLOWED_WHATSAPP_NUMBERS") or "").split(",")
    if n.strip()
}
# Set to "1" to skip Twilio signature checks while testing locally.
SKIP_SIGNATURE_VALIDATION = os.getenv("SKIP_SIGNATURE_VALIDATION") == "1"


def missing_required() -> list[str]:
    """Names of required settings that are absent. Checked at startup."""
    required = {
        "TWILIO_ACCOUNT_SID": TWILIO_ACCOUNT_SID,
        "TWILIO_AUTH_TOKEN": TWILIO_AUTH_TOKEN,
        "TWILIO_PHONE_NUMBER": TWILIO_PHONE_NUMBER,
        "OPENAI_API_KEY": OPENAI_API_KEY,
        "BASE_URL": BASE_URL,
    }
    return [name for name, value in required.items() if not value]
