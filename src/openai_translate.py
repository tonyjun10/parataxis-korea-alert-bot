"""
openai_translate.py — Translation via Anthropic Claude API.

Reuses the existing ANTHROPIC_API_KEY env var.
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

_API_URL = "https://api.anthropic.com/v1/messages"
_MODEL   = "claude-haiku-4-5-20251001"


class TranslateError(Exception):
    pass


def _translate_sync(text: str, target_lang: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise TranslateError("ANTHROPIC_API_KEY is not set.")

    lang_name = _LANG_NAMES.get(target_lang, target_lang)

    prompt = (
        f"Translate the following message into {lang_name}. "
        f"Preserve the original meaning, tone, formatting, line breaks, spacing, and emojis exactly. "
        f"Output only the translated text with no labels, quotes, explanations, or commentary.\n\n"
        f"{text}"
    )

    try:
        r = httpx.post(
            _API_URL,
            headers={
                "x-api-key":         api_key,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      _MODEL,
                "max_tokens": 2048,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=20,
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()
    except httpx.HTTPStatusError as exc:
        raise TranslateError(f"Claude API error {exc.response.status_code}: {exc.response.text[:200]}") from exc
    except Exception as exc:
        raise TranslateError(f"Translation request failed: {exc}") from exc


async def translate(text: str, target_lang: str) -> str:
    """Async entry point. Raises TranslateError on failure."""
    return await asyncio.to_thread(_translate_sync, text, target_lang)
