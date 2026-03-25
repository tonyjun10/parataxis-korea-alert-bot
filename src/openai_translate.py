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
_MODEL   = "claude-sonnet-4-6"


class TranslateError(Exception):
    pass


def _is_korean(text: str) -> bool:
    """Returns True if text contains significant Korean characters."""
    korean = sum(1 for c in text if '\uAC00' <= c <= '\uD7A3')
    alpha  = sum(1 for c in text if c.isalpha())
    return alpha > 0 and (korean / alpha) > 0.3


def _call_claude(system: str, text: str) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise TranslateError("ANTHROPIC_API_KEY is not set.")
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


def _translate_sync(text: str, target_lang: str | None) -> str:
    if target_lang is None:
        # Detect via Unicode — no API call needed
        target_lang = "en" if _is_korean(text) else "ko"
        log.info("[translate] auto target_lang=%s", target_lang)

    lang_name = _LANG_NAMES.get(target_lang, target_lang)
    system = (
        f"Translate the text the user sends into {lang_name}. "
        f"Output only the translated text. "
        f"Preserve formatting, line breaks, spacing, and emojis exactly. "
        f"No explanations, labels, or commentary."
    )
    return _call_claude(system, text)


async def detect_lang(text: str) -> str:
    """Kept for compatibility."""
    return "ko" if _is_korean(text) else "en"


async def translate(text: str, target_lang: str | None) -> str:
    """Async translation. target_lang=None means auto-detect and flip."""
    return await asyncio.to_thread(_translate_sync, text, target_lang)
