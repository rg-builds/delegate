"""Minimal OpenAI chat completions client. Raw HTTP, no SDK."""

import json

import httpx

from app import config

CHAT_SCHEMA_FORMAT = "json_schema"


async def complete(
    system: str,
    user: str,
    schema: dict | None = None,
    model: str | None = None,
) -> str | dict:
    """One chat completion.

    With `schema`, returns a parsed dict matching that JSON schema.
    Without it, returns the plain text reply.
    """
    payload: dict = {
        "model": model or config.OPENAI_TEXT_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }

    if schema:
        payload["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": "result", "strict": True, "schema": schema},
        }

    async with httpx.AsyncClient(timeout=60) as http:
        response = await http.post(
            config.OPENAI_CHAT_URL,
            headers={"Authorization": f"Bearer {config.OPENAI_API_KEY}"},
            json=payload,
        )

    if response.status_code >= 300:
        raise RuntimeError(f"OpenAI error {response.status_code}: {response.text[:400]}")

    content = response.json()["choices"][0]["message"]["content"]
    return json.loads(content) if schema else content
