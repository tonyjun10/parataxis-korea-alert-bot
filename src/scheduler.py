"""
scheduler.py — Background monitoring (PTB JobQueue).

Fixes applied:
  - Sends ONLY the single newest new item per company/category per run
    (not a batch of 5, which caused spam).
  - Recency guard: skips news items older than NEWS_MAX_AGE_DAYS (7).
  - Dedup is checked BEFORE sending, not after — no mark-and-forget races.
  - All fetched items are checked against dedup; only the best 1 is sent.
  - Full structured logging: chats, fetched, new, alerted counts per run.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import Application

import db
from dart import get_disclosures
from formatter import fmt_disclosures, fmt_news
from news import get_news
from brief import BriefError, take_screenshot_with_timeout
from luxor import LuxorError, fmt_mining_stats, get_mining_stats
import sheets as _sheets
from prices import fmt_stock_price, get_stock_price_krw, PARATAXIS_TICKER
from translate import summarize_article, translate_title

log  = logging.getLogger(__name__)
SEOUL = ZoneInfo("Asia/Seoul")

# ── Configuration ──────────────────────────────────────────────────────────────
NEWS_MAX_AGE_DAYS = 7   # do not alert on news older than this

_DART_COMPANIES = ["parataxis", "parataxiseth", "bitmax", "bitplanet"]
_NEWS_COMPANIES = ["parataxis", "parataxiseth", "bitmax", "bitplanet", "microstrategy"]

_COMPANY_LABEL = {
    "parataxis":     {"en": "Parataxis Korea", "ko": "파라택시스 코리아"},
    "bitmax":        {"en": "Bitmax",          "ko": "비트맥스"},
    "bitplanet":     {"en": "Bitplanet",       "ko": "비트플래닛"},
    "parataxiseth":  {"en": "Parataxis Ethereum", "ko": "파라택시스 이더리움"},
    "microstrategy": {"en": "Strategy",        "ko": "스트래티지"},
}


def _label(company: str, lang: str) -> str:
    return _COMPANY_LABEL.get(company, {}).get(lang, company.capitalize())


def register_jobs(app: Application, interval_minutes: int = 10):
    """Register the monitor job via PTB JobQueue. Call from main() before run_polling()."""
    app.job_queue.run_repeating(
        _monitor_job,
        interval=interval_minutes * 60,
        first=30,
        name="monitor",
    )
    log.info("Monitor job registered — every %d min, first run in 30 s.", interval_minutes)

    # Daily brief — 10:00 KST every day
    from datetime import time as dt_time
    app.job_queue.run_daily(
        _brief_job,
        time=dt_time(hour=10, minute=0, second=0, tzinfo=SEOUL),
        name="daily_brief",
    )
    log.info("Daily brief job registered — 10:00 KST.")

    # Daily snapshot — 9:00 KST every day
    app.job_queue.run_daily(
        _daily_job,
        time=dt_time(hour=9, minute=0, second=0, tzinfo=SEOUL),
        name="daily_snapshot",
    )
    log.info("Daily snapshot job registered — 9:00 KST.")

    # Mining update — every 10 min alongside monitor
    app.job_queue.run_repeating(
        _mining_job,
        interval=interval_minutes * 60,
        first=60,
        name="mining",
    )
    log.info("Mining job registered — every %d min.", interval_minutes)


async def _monitor_job(context) -> None:
    bot: Bot = context.bot
    now_str = datetime.now(SEOUL).strftime("%Y-%m-%d %H:%M:%S KST")
    log.info("=" * 60)
    log.info("MONITOR RUN START  %s", now_str)
    log.info("=" * 60)

    total_alerted = 0
    try:
        # Run all disclosure + news checks in parallel across companies
        disc_tasks = [_check_disclosures(bot, company) for company in _DART_COMPANIES]
        news_tasks = [_check_news(bot, company)        for company in _NEWS_COMPANIES]
        results = await asyncio.gather(*disc_tasks, *news_tasks, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                log.error("Monitor task error: %s", r)
            else:
                total_alerted += r
    except Exception:
        log.exception("Unhandled exception in monitor job")

    log.info("MONITOR RUN COMPLETE — total alerts sent: %d", total_alerted)


# ── Disclosure check ───────────────────────────────────────────────────────────

async def _check_disclosures(bot: Bot, company: str) -> int:
    chats = db.get_chats_for(company, "disclosures")
    log.info("[disc/%s] subscribed chats: %d", company, len(chats))
    if not chats:
        return 0

    try:
        items = await get_disclosures(company, limit=10)
    except Exception as e:
        log.error("[disc/%s] fetch exception: %s", company, e, exc_info=True)
        return 0

    if not items:
        log.info("[disc/%s] fetched: 0", company)
        return 0
    if "error" in items[0]:
        log.warning("[disc/%s] API error: %s", company, items[0]["error"])
        return 0

    log.info("[disc/%s] fetched: %d", company, len(items))

    # Filter to new items (company-aware dedup)
    new_items = [
        it for it in items
        if it.get("rcept_no") and db.is_new_disclosure(it["rcept_no"], company)
    ]
    log.info("[disc/%s] new: %d", company, len(new_items))

    if not new_items:
        return 0

    # Sort by date descending, pick the single newest
    def _disc_key(it):
        return it.get("pub_date", it.get("date", "")) or ""
    new_items.sort(key=_disc_key, reverse=True)
    best = new_items[0]

    # Log to watchlist sheet (fire-and-forget)
    disc_title = best.get("title", best.get("report_nm", ""))
    disc_url   = best.get("url", "")
    asyncio.create_task(_sheets.append_watchlist_entry(company, "Disclosure", disc_title, disc_url))

    alerted = 0
    for chat in chats:
        chat_id = chat["chat_id"]
        lang    = chat.get("lang", "en")
        label   = _label(company, lang)
        header  = (
            f"🔔 <b>새 공시 — {label}</b>\n\n"
            if lang == "ko" else
            f"🔔 <b>New Disclosure — {label}</b>\n\n"
        )
        if await _send(bot, chat_id, header + fmt_disclosures([best], lang)):
            alerted += 1

    log.info("[disc/%s] alerted: %d chat(s)", company, alerted)
    return alerted


# ── News check ─────────────────────────────────────────────────────────────────

def _parse_item_dt(item: dict) -> datetime | None:
    """Parse the 'time' field (YYYY-MM-DD HH:MM) into a datetime, or None."""
    time_str = item.get("time", "")
    if not time_str:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(time_str, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


async def _check_news(bot: Bot, company: str) -> int:
    chats = db.get_chats_for(company, "news")
    log.info("[news/%s] subscribed chats: %d", company, len(chats))
    if not chats:
        return 0

    try:
        items = await get_news(company, limit=10)
    except Exception as e:
        log.error("[news/%s] fetch exception: %s", company, e, exc_info=True)
        return 0

    log.info("[news/%s] fetched: %d", company, len(items))

    cutoff = datetime.now(timezone.utc) - timedelta(days=NEWS_MAX_AGE_DAYS)
    recent_items = []
    skipped_old  = 0

    for it in items:
        url = it.get("url", "")
        if not url:
            continue
        dt = _parse_item_dt(it)
        if dt is not None and dt < cutoff:
            skipped_old += 1
            # Still mark as seen so it never resurfaces
            db.mark_news_seen(url, company)
            continue
        recent_items.append(it)

    if skipped_old:
        log.info("[news/%s] skipped %d old items (>%dd)", company, skipped_old, NEWS_MAX_AGE_DAYS)

    new_items = [
        it for it in recent_items
        if db.is_new_news(it["url"], company)
    ]
    log.info("[news/%s] new (within age limit): %d", company, len(new_items))

    if not new_items:
        return 0

    # Sort by published time descending (items without time go last)
    def _news_key(it):
        dt = _parse_item_dt(it)
        return dt if dt is not None else datetime.min.replace(tzinfo=timezone.utc)
    new_items.sort(key=_news_key, reverse=True)
    best = new_items[0]

    # Log to watchlist sheet (fire-and-forget)
    asyncio.create_task(_sheets.append_watchlist_entry(
        company, "News", best.get("title", ""), best.get("url", "")))

    # ── Translate title + optional summary (done once, reused for all chats) ──
    original_title = best.get("title", "")
    url            = best.get("url", "")
    publisher      = best.get("publisher", "")
    time_str       = best.get("time", "")

    # Translate for both languages in parallel (summaries disabled)
    en_title, ko_title = await asyncio.gather(
        asyncio.to_thread(translate_title, original_title, "en"),
        asyncio.to_thread(translate_title, original_title, "ko"),
    )
    translated = {"en": en_title, "ko": ko_title}
    summaries  = {"en": None, "ko": None}

    alerted = 0
    for chat in chats:
        chat_id = chat["chat_id"]
        lang    = chat.get("lang", "en")
        label   = _label(company, lang)

        msg = _fmt_news_alert(
            lang=lang,
            label=label,
            original_title=original_title,
            translated_title=translated.get(lang),
            publisher=publisher,
            time_str=time_str,
            url=url,
            summary=summaries.get(lang),
        )
        if await _send(bot, chat_id, msg):
            alerted += 1

    log.info("[news/%s] alerted: %d chat(s)", company, alerted)
    return alerted


# ── News alert formatter (with translation + optional summary) ────────────────

def _fmt_news_alert(
    lang: str,
    label: str,
    original_title: str,
    translated_title: str | None,
    publisher: str,
    time_str: str,
    url: str,
    summary: str | None,
) -> str:
    from html import escape

    if lang == "ko":
        header     = f"🔔 <b>새 기사 — {label}</b>"
        orig_label = "원본 제목"
        xlat_label = "한국어 제목"
    else:
        header     = f"🔔 <b>New News — {label}</b>"
        orig_label = "Original Title"
        xlat_label = "English Title"

    lines = [header, ""]
    lines.append(f"{orig_label}: {escape(original_title)}")

    if translated_title and translated_title.strip() != original_title.strip():
        lines.append(f"{xlat_label}: {escape(translated_title)}")

    if publisher:
        pub_label = "출처" if lang == "ko" else "Source"
        lines.append(f"{pub_label}: {escape(publisher)}")

    if time_str:
        lines.append(f"🕐 {time_str}")

    if summary:
        sum_label = "요약" if lang == "ko" else "Summary"
        lines.append(f"\n📝 <b>{sum_label}:</b> {escape(summary)}")

    lines.append(f"\n🔗 <a href=\"{url}\">기사 보기</a>" if lang == "ko"
                 else f"\n🔗 <a href=\"{url}\">Read Article</a>")

    return "\n".join(lines)


# ── Send helper ────────────────────────────────────────────────────────────────

async def _send(bot: Bot, chat_id: int, text: str) -> bool:
    """Send message. Returns True on success."""
    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        log.info("Alert sent → chat %d", chat_id)
        return True
    except Exception as e:
        log.warning("Failed to send alert → chat %d: %s", chat_id, e)
        return False


# ── Daily brief job ────────────────────────────────────────────────────────────

async def _brief_job(context) -> None:
    """
    Runs at 10:00 KST daily via PTB JobQueue.run_daily().

    Takes ONE screenshot per language (en/ko), then reuses the same bytes for
    every subscribed chat of that language. This avoids launching N browser
    instances for N subscribers.
    """
    bot: Bot = context.bot
    now_str  = datetime.now(SEOUL).strftime("%Y-%m-%d %H:%M:%S KST")
    log.info("── BRIEF JOB START  %s ──", now_str)

    chats = db.get_chats_for("brief", "brief")
    log.info("[brief] subscribed chats: %d", len(chats))
    if not chats:
        log.info("[brief] no subscribers — skipping.")
        return

    # Partition chats by language
    en_chats = [c for c in chats if c.get("lang", "en") != "ko"]
    ko_chats = [c for c in chats if c.get("lang", "en") == "ko"]
    log.info("[brief] en=%d  ko=%d", len(en_chats), len(ko_chats))

    # Take screenshots (at most one per language)
    screenshots: dict[str, bytes | None] = {"en": None, "ko": None}
    errors:      dict[str, str]          = {}

    for lang, group in (("en", en_chats), ("ko", ko_chats)):
        if not group:
            continue
        try:
            screenshots[lang] = await take_screenshot_with_timeout(lang)
            log.info("[brief/%s] screenshot captured (%d bytes)", lang, len(screenshots[lang]))
        except BriefError as exc:
            errors[lang] = str(exc)
            log.error("[brief/%s] screenshot failed: %s", lang, exc)

    # Deliver to each chat
    alerted = 0
    for chat in chats:
        chat_id = chat["chat_id"]
        lang    = chat.get("lang", "en")
        png     = screenshots.get(lang)

        if png is None:
            err = errors.get(lang, "Unknown error")
            err_msg = (
                f"⚠️ 오늘의 브리프를 가져오지 못했습니다: {err}"
                if lang == "ko" else
                f"⚠️ Could not capture today's brief: {err}"
            )
            if await _send(bot, chat_id, err_msg):
                alerted += 1
            continue

        caption = (
            f"📊 데일리 마켓 대시보드 — {datetime.now(SEOUL).strftime('%Y-%m-%d')} 오전 10시"
            if lang == "ko" else
            f"📊 Daily Market Dashboard — {datetime.now(SEOUL).strftime('%Y-%m-%d')} 10:00 KST"
        )
        if await _send_photo(bot, chat_id, png, caption):
            alerted += 1

    log.info("[brief] delivered to %d chat(s)", alerted)
    log.info("── BRIEF JOB COMPLETE ──")


async def _send_photo(bot: Bot, chat_id: int, png_bytes: bytes, caption: str) -> bool:
    """Send a photo. Returns True on success."""
    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=png_bytes,
            caption=caption,
        )
        log.info("Brief photo sent → chat %d", chat_id)
        return True
    except Exception as exc:
        log.warning("Failed to send brief photo → chat %d: %s", chat_id, exc)
        return False

# ── Mining job ────────────────────────────────────────────────────────────────

async def _mining_job(context) -> None:
    """
    Runs every 10 min. Fetches mining stats and sends to all /watch mining
    subscribers. Only sends if the stats have materially changed since last
    run (hashrate changes by >1% or worker count changes) to avoid spam.
    """
    bot: Bot = context.bot
    log.info("── MINING JOB START ──")

    chats = db.get_chats_for("mining", "mining")
    log.info("[mining] subscribed chats: %d", len(chats))
    if not chats:
        return

    try:
        stats = await get_mining_stats()
    except LuxorError as exc:
        log.error("[mining] fetch failed: %s", exc)
        return

    # Change detection — store last values in job context data
    data        = context.job.data or {}
    last_hr     = data.get("hashrate_ph", -1.0)
    last_workers = data.get("active_workers", -1)

    hr_changed = last_hr < 0 or abs(stats.hashrate_ph - last_hr) / max(last_hr, 0.0001) > 0.01
    wk_changed = last_workers < 0 or stats.active_workers != last_workers

    if not (hr_changed or wk_changed):
        log.info("[mining] no material change — skipping alert")
        return

    context.job.data = {
        "hashrate_ph":    stats.hashrate_ph,
        "active_workers": stats.active_workers,
    }

    alerted = 0
    for chat in chats:
        chat_id = chat["chat_id"]
        lang    = chat.get("lang", "en")
        if await _send(bot, chat_id, fmt_mining_stats(stats, lang)):
            alerted += 1

    log.info("[mining] alerted %d chat(s)", alerted)
    log.info("── MINING JOB COMPLETE ──")


# ── Daily snapshot job ─────────────────────────────────────────────────────────

async def _daily_job(context) -> None:
    """
    Runs at 9:00 KST daily. Sends brief screenshot + mining stats + stock price
    to all /watch daily subscribers.
    """
    bot: Bot = context.bot
    now_str  = datetime.now(SEOUL).strftime("%Y-%m-%d %H:%M:%S KST")
    log.info("── DAILY JOB START  %s ──", now_str)

    chats = db.get_chats_for("daily", "daily")
    log.info("[daily] subscribed chats: %d", len(chats))
    if not chats:
        log.info("[daily] no subscribers — skipping.")
        return

    # Partition by language; take one screenshot per language
    en_chats = [c for c in chats if c.get("lang", "en") != "ko"]
    ko_chats = [c for c in chats if c.get("lang", "en") == "ko"]

    screenshots: dict[str, bytes | None] = {"en": None, "ko": None}
    for lang, group in (("en", en_chats), ("ko", ko_chats)):
        if not group:
            continue
        try:
            screenshots[lang] = await take_screenshot_with_timeout(lang)
            log.info("[daily/%s] screenshot captured (%d bytes)", lang, len(screenshots[lang]))
        except BriefError as exc:
            log.error("[daily/%s] screenshot failed: %s", lang, exc)

    # Fetch mining stats and stock price once for all chats
    try:
        stats = await get_mining_stats()
    except LuxorError as exc:
        log.error("[daily] mining fetch failed: %s", exc)
        stats = None

    try:
        stock = await get_stock_price_krw(PARATAXIS_TICKER)
    except Exception as exc:
        log.error("[daily] stock fetch failed: %s", exc)
        stock = None

    date_str = datetime.now(SEOUL).strftime("%Y-%m-%d")
    alerted = 0

    for chat in chats:
        chat_id = chat["chat_id"]
        lang    = chat.get("lang", "en")
        png     = screenshots.get(lang)

        # 1. Brief screenshot
        if png:
            caption = (
                f"📊 데일리 마켓 대시보드 — {date_str}"
                if lang == "ko" else
                f"📊 Daily Market Dashboard — {date_str}"
            )
            await _send_photo(bot, chat_id, png, caption)
        else:
            err = "⚠️ 오늘의 대시보드 스크린샷을 가져오지 못했습니다." if lang == "ko" else "⚠️ Could not capture today's dashboard screenshot."
            await _send(bot, chat_id, err)

        # 2. Mining stats
        if stats:
            await _send(bot, chat_id, fmt_mining_stats(stats, lang))
        else:
            err = "⚠️ 채굴 데이터를 가져오지 못했습니다." if lang == "ko" else "⚠️ Could not fetch mining stats."
            await _send(bot, chat_id, err)

        # 3. Stock price
        if stock:
            await _send(bot, chat_id, fmt_stock_price(stock, lang))
        else:
            err = "⚠️ 주가를 가져오지 못했습니다." if lang == "ko" else "⚠️ Could not fetch stock price."
            await _send(bot, chat_id, err)

        alerted += 1

    log.info("[daily] delivered to %d chat(s)", alerted)
    log.info("── DAILY JOB COMPLETE ──")
