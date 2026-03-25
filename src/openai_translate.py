"""
openai_translate.py — Translation via Anthropic Claude API.

Reuses the existing ANTHROPIC_API_KEY env var.
All errors surface as TranslateError so callers can handle gracefully.

If target_lang is None, Claude auto-detects and translates to the opposite language
(English → Korean, Korean → English) in a single API call.
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


def _translate_sync(text: str, target_lang: str | None) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise TranslateError("ANTHROPIC_API_KEY is not set.")

    if target_lang is None:
        system = (
            "You are a translation engine. "
            "If the input is in Korean, translate it to English. "
            "If the input is in English, translate it to Korean. "
            "Always translate the entire message including any mixed words. "
            "Output only the translated text. No explanations or commentary."
        )
    else:
        lang_name = _LANG_NAMES.get(target_lang, target_lang)
        system = (
            f"You are a translation engine. Translate everything the user sends into {lang_name}. "
            f"Output only the translated text. "
            f"Preserve formatting, line breaks, spacing, and emojis exactly. "
            f"No explanations, labels, quotes, or commentary."
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
                "system":     system,
                "messages":   [{"role": "user", "content": text}],
            },
            timeout=20,
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()
    except httpx.HTTPStatusError as exc:
        raise TranslateError(f"Claude API error {exc.response.status_code}: {exc.response.text[:200]}") from exc
    except Exception as exc:
        raise TranslateError(f"Translation request failed: {exc}") from exc


async def detect_lang(text: str) -> str:
    """Kept for compatibility — not used by auto-detect path."""
    return "en"


async def translate(text: str, target_lang: str | None) -> str:
    """Async translation. target_lang=None means auto-detect and flip."""
    return await asyncio.to_thread(_translate_sync, text, target_lang)
