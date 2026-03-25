"""
openai_translate.py — Translation via OpenAI API.

Reads OPENAI_API_KEY and optionally OPENAI_MODEL from environment.
All errors surface as TranslateError so callers can handle gracefully.
"""

import asyncio
import logging
import os

import httpx

log = logging.getLogger(__name__)

_LANG_NAMES = {
    "en": "English",
    "ko": "Korean",
}

SUPPORTED_LANGS = set(_LANG_NAMES.keys())


class TranslateError(Exception):
    pass


def _translate_sync(text: str, target_lang: str) -> str:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        raise TranslateError("OPENAI_API_KEY is not set.")

    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    lang_name = _LANG_NAMES.get(target_lang, target_lang)

    system_prompt = (
        f"You are a professional translator. "
        f"Translate the user's message into {lang_name}. "
        f"Preserve the original meaning, tone, formatting, line breaks, spacing, and emojis exactly. "
        f"Output only the translated text with no labels, quotes, explanations, or commentary."
    )

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": text},
        ],
        "temperature": 0.3,
    }

    with httpx.Client(timeout=20) as client:
        r = client.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            json=payload,
        )

    if r.status_code != 200:
        raise TranslateError(f"OpenAI API error {r.status_code}: {r.text[:200]}")

    data = r.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError) as exc:
        raise TranslateError(f"Unexpected API response: {exc}") from exc


async def translate(text: str, target_lang: str) -> str:
    """Async entry point. Raises TranslateError on failure."""
    return await asyncio.to_thread(_translate_sync, text, target_lang)
