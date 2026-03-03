"""
scheduler.py — Background monitoring every 10 minutes.

FIX: The previous implementation used a lambda inside add_job(), which is
unreliable on Railway because APScheduler calls it synchronously and
app.create_task() may not be available at call time.

The correct pattern for python-telegram-bot v20+ is to use the
post_init hook to wire up the scheduler using the application's
job_queue, OR to use asyncio directly with run_coroutine_threadsafe.

Here we use JobQueue (built into python-telegram-bot) which is the
recommended approach and works correctly on Railway.

Companies and categories monitored:
  parataxis     → disclosures + news
  bitmax        → disclosures + news
  bitplanet     → disclosures + news
  microstrategy → news only
"""

import logging

from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import Application

import db
from dart import get_disclosures
from formatter import fmt_disclosures, fmt_news
from news import get_news

log = logging.getLogger(__name__)

_DART_COMPANIES = ["parataxis", "bitmax", "bitplanet"]
_NEWS_COMPANIES = ["parataxis", "bitmax", "bitplanet", "microstrategy"]

_COMPANY_LABEL = {
    "parataxis":     {"en": "Parataxis Korea", "ko": "파라택시스 코리아"},
    "bitmax":        {"en": "Bitmax",          "ko": "비트맥스"},
    "bitplanet":     {"en": "Bitplanet",       "ko": "비트플래닛"},
    "microstrategy": {"en": "Strategy",        "ko": "스트래티지"},
}


def _label(company: str, lang: str) -> str:
    return _COMPANY_LABEL.get(company, {}).get(lang, company.capitalize())


def register_jobs(app: Application, interval_minutes: int = 10):
    """
    Register the monitor job using PTB's built-in JobQueue.
    Call this from main() BEFORE app.run_polling().
    """
    app.job_queue.run_repeating(
        _monitor_job,
        interval=interval_minutes * 60,
        first=30,  # wait 30s after startup before first run
        name="monitor",
    )
    log.info("Scheduler registered — monitor every %d min (first run in 30s).", interval_minutes)


async def _monitor_job(context) -> None:
    """Entry point called by PTB JobQueue — runs in the event loop."""
    bot: Bot = context.bot
    log.info("=" * 50)
    log.info("MONITOR RUN STARTED")
    log.info("=" * 50)

    try:
        for company in _DART_COMPANIES:
            await _check_disclosures(bot, company)

        for company in _NEWS_COMPANIES:
            await _check_news(bot, company)

    except Exception as e:
        log.error("MONITOR RUN ERROR: %s", e, exc_info=True)

    log.info("MONITOR RUN COMPLETE")


async def _check_disclosures(bot: Bot, company: str):
    chats = db.get_chats_for(company, "disclosures")
    log.info("[disclosures/%s] %d subscribed chats", company, len(chats))
    if not chats:
        return

    try:
        items = await get_disclosures(company, limit=10)
    except Exception as e:
        log.error("[disclosures/%s] fetch error: %s", company, e)
        return

    if not items:
        log.info("[disclosures/%s] 0 items fetched", company)
        return
    if "error" in items[0]:
        log.warning("[disclosures/%s] API error: %s", company, items[0]["error"])
        return

    log.info("[disclosures/%s] %d items fetched", company, len(items))

    new_items = [it for it in items if db.is_new_disclosure(it["rcept_no"], company)]
    log.info("[disclosures/%s] %d NEW items", company, len(new_items))

    if not new_items:
        return

    for chat in chats:
        chat_id = chat["chat_id"]
        lang    = chat.get("lang", "en")
        label   = _label(company, lang)
        header  = (
            f"🔔 <b>새 공시 — {label}</b>\n\n"
            if lang == "ko" else
            f"🔔 <b>New Disclosure — {label}</b>\n\n"
        )
        await _send(bot, chat_id, header + fmt_disclosures(new_items[:5], lang))


async def _check_news(bot: Bot, company: str):
    chats = db.get_chats_for(company, "news")
    log.info("[news/%s] %d subscribed chats", company, len(chats))
    if not chats:
        return

    try:
        items = await get_news(company, limit=10)
    except Exception as e:
        log.error("[news/%s] fetch error: %s", company, e)
        return

    log.info("[news/%s] %d items fetched", company, len(items))

    new_items = [it for it in items if it.get("url") and db.is_new_news(it["url"], company)]
    log.info("[news/%s] %d NEW items", company, len(new_items))

    if not new_items:
        return

    for chat in chats:
        chat_id = chat["chat_id"]
        lang    = chat.get("lang", "en")
        label   = _label(company, lang)
        header  = (
            f"🔔 <b>새 기사 — {label}</b>\n\n"
            if lang == "ko" else
            f"🔔 <b>New News — {label}</b>\n\n"
        )
        await _send(bot, chat_id, header + fmt_news(new_items[:5], lang))


async def _send(bot: Bot, chat_id: int, text: str):
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        log.info("Alert sent to chat %d", chat_id)
    except Exception as e:
        log.warning("Failed to send alert to %d: %s", chat_id, e)
