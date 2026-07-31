from google import genai
from dotenv import load_dotenv
import os

load_dotenv()

MODEL = "gemini-3.1-flash-live-preview"

client = genai.Client(
    api_key=os.getenv("GEMINI_API_KEY")
)