"""
scheduler.py — Background monitoring every 10 minutes.
Respects per-chat category subscriptions and delivers alerts in the chat's language.

Companies monitored:
  - bitmax      → disclosures (DART) + news (RSS/GDELT)
  - bitplanet   → disclosures (DART) + news (RSS/GDELT)
  - microstrategy → news only (RSS/GDELT, no DART)
"""

import logging
from telegram import Bot
from telegram.constants import ParseMode

from db import (
    get_chats_for_category,
    is_new_disclosure, is_new_news,
    get_lang,
)
from dart import get_disclosures
from news import get_news
from formatter import fmt_disclosures, fmt_news

log = logging.getLogger(__name__)

# Companies with DART disclosures
_DART_COMPANIES  = ["bitmax", "bitplanet"]

# All companies monitored for news
_NEWS_COMPANIES  = ["bitmax", "bitplanet", "microstrategy"]


async def run_monitor(bot: Bot):
    """Called every 10 minutes by the APScheduler job."""
    log.info("Monitor run started.")

    for company in _DART_COMPANIES:
        await _check_disclosures(bot, company)

    for company in _NEWS_COMPANIES:
        await _check_news(bot, company)


# ── Disclosures (Bitmax + Bitplanet only) ─────────────────────────────────────

async def _check_disclosures(bot: Bot, company: str):
    chats = get_chats_for_category("disclosures")
    if not chats:
        return

    items = get_disclosures(company, limit=10)
    if not items or "error" in items[0]:
        return

    new_items = [it for it in items if is_new_disclosure(it["rcept_no"], company)]
    if not new_items:
        return

    for chat in chats:
        chat_id = chat["chat_id"]
        lang    = chat.get("language") or get_lang(chat_id)
        label   = _company_display(company, lang)
        header  = (
            f"🔔 <b>새 공시 — {label}</b>\n\n"
            if lang == "ko" else
            f"🔔 <b>New Disclosure — {label}</b>\n\n"
        )
        await _send(bot, chat_id, header + fmt_disclosures(new_items[:5], lang))


# ── News (all three companies) ────────────────────────────────────────────────

async def _check_news(bot: Bot, company: str):
    chats = get_chats_for_category("news")
    if not chats:
        return

    items = get_news(company, limit=10)
    if not items:
        return

    new_items = [it for it in items if is_new_news(it["url"], company)]
    if not new_items:
        return

    for chat in chats:
        chat_id = chat["chat_id"]
        lang    = chat.get("language") or get_lang(chat_id)
        label   = _company_display(company, lang)
        header  = (
            f"🔔 <b>새 기사 — {label}</b>\n\n"
            if lang == "ko" else
            f"🔔 <b>New News — {label}</b>\n\n"
        )
        await _send(bot, chat_id, header + fmt_news(new_items[:5], lang))


# ── Helpers ───────────────────────────────────────────────────────────────────

def _company_display(key: str, lang: str) -> str:
    mapping = {
        "bitmax":        {"en": "Bitmax",    "ko": "비트맥스"},
        "bitplanet":     {"en": "Bitplanet", "ko": "비트플래닛"},
        "microstrategy": {"en": "Strategy",  "ko": "스트래티지"},
    }
    return mapping.get(key, {}).get(lang, key.capitalize())


async def _send(bot: Bot, chat_id: int, text: str):
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    except Exception as e:
        log.warning("Failed to send alert to %d: %s", chat_id, e)