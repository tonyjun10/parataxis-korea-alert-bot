"""
translate.py — Lightweight title translation + optional article summary.

Uses the Anthropic API (claude-haiku — fast and cheap).
All functions fail gracefully: on any error, return None so callers
can fall back to the original title/no summary.

Requires env var: ANTHROPIC_API_KEY
"""

import logging
import os
import re

import httpx

log = logging.getLogger(__name__)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
API_URL           = "https://api.anthropic.com/v1/messages"
MODEL             = "claude-haiku-4-5-20251001"  # correct Anthropic native API model string
TIMEOUT           = 10   # seconds — keep alerts snappy
FETCH_TIMEOUT     = 8    # seconds for article fetch
MAX_ARTICLE_CHARS = 4000 # truncate before sending to Claude
MAX_SUMMARY_WORDS = 60   # target summary length


def _claude(prompt: str, max_tokens: int = 200) -> str | None:
    """Call Claude API. Returns text response or None on any failure."""
    if not ANTHROPIC_API_KEY:
        log.warning("[translate] ANTHROPIC_API_KEY not set — skipping.")
        return None
    try:
        r = httpx.post(
            API_URL,
            headers={
                "x-api-key":         ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type":      "application/json",
            },
            json={
                "model":      MODEL,
                "max_tokens": max_tokens,
                "messages":   [{"role": "user", "content": prompt}],
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        return r.json()["content"][0]["text"].strip()
    except httpx.HTTPStatusError as exc:
        log.warning("[translate] Claude API %d: %s", exc.response.status_code, exc.response.text[:500])
        return None
    except Exception as exc:
        log.warning("[translate] Claude API call failed: %s", exc)
        return None


def translate_title(title: str, target_lang: str) -> str | None:
    """
    Translate a news headline into target_lang ("en" or "ko").
    Returns translated string, or None if translation fails.
    If the title is already in target_lang, Claude will return it as-is.
    """
    if not title:
        return None

    lang_name = "English" if target_lang == "en" else "Korean"
    prompt = (
        f"Translate the following news headline into {lang_name}. "
        f"Return ONLY the translated headline, nothing else.\n\n"
        f"Headline: {title}"
    )
    result = _claude(prompt, max_tokens=150)
    if result:
        log.info("[translate] title translated (%s): %s", target_lang, result[:80])
    return result


def _fetch_article_text(url: str) -> str | None:
    """
    Fetch article page and extract readable text.
    Returns plain text (truncated) or None on any failure.
    Robust — handles timeouts, bot blocks, and bad HTML gracefully.
    """
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9,ko;q=0.8",
        }
        r = httpx.get(url, headers=headers, timeout=FETCH_TIMEOUT, follow_redirects=True)
        r.raise_for_status()

        # Strip HTML tags with a simple regex — no BeautifulSoup dependency
        html  = r.text
        # Remove script/style blocks
        html  = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        # Remove all remaining tags
        text  = re.sub(r"<[^>]+>", " ", html)
        # Collapse whitespace
        text  = re.sub(r"\s+", " ", text).strip()
        return text[:MAX_ARTICLE_CHARS] if text else None

    except Exception as exc:
        log.info("[translate] article fetch failed (%s): %s", url, exc)
        return None


def summarize_article(url: str, target_lang: str) -> str | None:
    """
    Phase 2 — disabled for now.
    Google News URLs redirect to consent pages so article extraction fails.
    Returns None always so alerts fall back to title-only.
    """
    return None
