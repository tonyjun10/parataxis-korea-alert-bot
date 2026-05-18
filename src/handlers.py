"""
handlers.py — All Telegram command and callback handlers.

Fixes applied:
  - /watch and approval now call seed_seen_for_chat() to pre-populate
    dedup tables with current items, preventing backlog spam on first run.
  - All fetch calls are properly awaited (dart + news are async).
"""

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

import db
from dart import get_disclosures
from formatter import fmt_disclosures, fmt_news
from keyboards import (
    kb_after_price, kb_after_result, kb_approval,
    kb_category, kb_language, kb_main, kb_price, kb_subscribe,
    kb_subscribe_persistent, kb_unwatch_categories, kb_watch_categories,
)
from news import get_news
from prices import fmt_price, fmt_stock_price, get_price, get_price_usd, get_stock_price_krw, PARATAXIS_TICKER
from brief import BriefError, take_screenshot_with_timeout
from luxor import LuxorError, fmt_mining_stats, get_mining_stats

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
ADMIN_USER_ID: int = 7205462694

_DART_COMPANIES = {"parataxis", "parataxiseth", "bitmax", "bitplanet"}
_ALL_COMPANIES  = ["parataxis", "parataxiseth", "bitmax", "bitplanet", "microstrategy"]

_COMPANY_LABEL = {
    "parataxis":     {"en": "Parataxis Korea", "ko": "파라택시스 코리아"},
    "bitmax":        {"en": "Bitmax",          "ko": "비트맥스"},
    "bitplanet":     {"en": "Bitplanet",       "ko": "비트플래닛"},
    "parataxiseth":  {"en": "Parataxis Ethereum", "ko": "파라택시스 이더리움"},
    "microstrategy": {"en": "Strategy",        "ko": "스트래티지"},
}


def _is_admin(user_id: int | None) -> bool:
    return user_id == ADMIN_USER_ID


def _company_label(key: str, lang: str) -> str:
    return _COMPANY_LABEL.get(key, {}).get(lang, key)


def _main_prompt(lang: str) -> str:
    return "메뉴를 선택하세요:" if lang == "ko" else "Select a menu:"


# ── Seeding helper — call after any new subscription ──────────────────────────

async def _seed_dedup_tables():
    """
    Fetch current top items for all companies and mark them as seen
    WITHOUT sending alerts. Prevents backlog spam after a fresh subscribe.
    Errors are swallowed — seeding is best-effort.
    """
    log.info("Seeding dedup tables for new subscription…")
    news_by_company: dict[str, list[dict]] = {}
    disc_by_company: dict[str, list[dict]] = {}

    for company in _ALL_COMPANIES:
        try:
            items = await get_news(company, limit=10)
            news_by_company[company] = items
        except Exception as e:
            log.warning("Seed news fetch failed for %s: %s", company, e)
            news_by_company[company] = []

    for company in _DART_COMPANIES:
        try:
            items = await get_disclosures(company, limit=10)
            disc_by_company[company] = [it for it in items if "error" not in it]
        except Exception as e:
            log.warning("Seed disc fetch failed for %s: %s", company, e)
            disc_by_company[company] = []

    db.seed_seen_for_chat(news_by_company, disc_by_company)
    log.info("Dedup seeding complete.")


# ── /start — approval gate ─────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    chat_id = update.effective_chat.id
    db.log_event("start", user.id, user.username, chat_id)

    if _is_admin(user.id):
        db.approve_chat(chat_id)
        await _show_language_prompt(update)
        return

    if db.is_approved(chat_id):
        await _show_language_prompt(update)
        return

    db.request_access(chat_id)
    await update.message.reply_text(
        "🔒 Access restricted. Your request has been sent to the administrator.",
        parse_mode=ParseMode.HTML,
    )

    username  = f"@{user.username}" if user.username else f"id:{user.id}"
    name      = user.full_name or ""
    admin_msg = (
        f"🔐 <b>Access Request</b>\n\n"
        f"User: {username} ({name})\n"
        f"Chat ID: <code>{chat_id}</code>\n\n"
        f"Approve or deny below:"
    )
    try:
        await ctx.bot.send_message(
            chat_id=ADMIN_USER_ID,
            text=admin_msg,
            parse_mode=ParseMode.HTML,
            reply_markup=kb_approval(chat_id),
        )
    except Exception as e:
        log.warning("Could not notify admin of access request: %s", e)


async def _show_language_prompt(update: Update):
    """Show unified welcome + feature list + language selection in one message."""
    await update.message.reply_text(
        "👋 <b>Welcome to the Parataxis Family Bot</b>\n\n"
        "🤖 <b>What you can do with this bot:</b>\n"
        "• 📊 /subscribe — Subscribe to daily updates (BTC/ETH prices, stock prices, news feeds)\n"
        "• 📅 /daily — Daily snapshot of prices, stocks &amp; mining stats\n"
        "• 📸 /brief — Live BTC &amp; ETH dashboard screenshots\n"
        "• ⛏️ /mining — Current mining stats\n"
        "• 🌐 /t — Translate Korean↔English automatically\n"
        "• 📋 /kakaoexport — Summary of the Kakao shareholder groupchat\n\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🤖 <b>이 봇으로 할 수 있는 것:</b>\n"
        "• 📊 /subscribe — 데일리 업데이트 구독 (BTC/ETH 가격, 주식, 뉴스)\n"
        "• 📅 /daily — 가격, 주식 &amp; 채굴 현황 데일리 스냅샷\n"
        "• 📸 /brief — BTC &amp; ETH 라이브 대시보드 스크린샷\n"
        "• ⛏️ /mining — 현재 채굴 현황\n"
        "• 🌐 /t — 한국어↔영어 자동 번역\n"
        "• 📋 /kakaoexport — 주주 단체 카카오톡 요약 확인\n\n"
        "Select your preferred language / 언어를 선택해 주세요:",
        reply_markup=kb_language(),
        parse_mode=ParseMode.HTML,
    )


# ── /help ─────────────────────────────────────────────────────────────────────

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lang = db.get_lang(update.effective_chat.id)
    if lang == "ko":
        text = (
            "<b>📖 도움말</b>\n\n"
            "• /start — 시작 및 언어 선택\n"
            "• /subscribe — 구독 관리 (체크박스 메뉴)\n"
            "• /status — 구독 상태 확인\n"
            "• /brief — BTC + ETH 대시보드 스크린샷\n"
            "• /daily — 데일리 스냅샷 (가격 + 주식 + 채굴)\n"
            "• /mining — 채굴 현황 보기\n"
            "• /fx — USD/KRW 환율 확인\n"
            "• /t [텍스트] — 한국어↔영어 번역\n"
            "• /help — 이 도움말"
        )
    else:
        text = (
            "<b>📖 Help</b>\n\n"
            "• /start — Welcome screen &amp; language selection\n"
            "• /subscribe — Manage subscriptions (checkbox menu)\n"
            "• /status — Check your current subscriptions\n"
            "• /brief — BTC + ETH dashboard screenshots\n"
            "• /daily — Daily snapshot (prices + stocks + mining)\n"
            "• /mining — Get mining stats right now\n"
            "• /fx — Check USD/KRW exchange rate\n"
            "• /t [text] — Translate Korean↔English\n"
            "• /help — This message"
        )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)



# ── Subscription key mapping ──────────────────────────────────────────────────

_SUB2_MAP = {
    # sub2 key -> list of (company, category) pairs to subscribe/unsubscribe
    "coin_prices":    [("brief", "brief")],          # reuses brief slot for price alerts
    "stock_prices":   [("daily", "daily")],           # reuses daily slot for stock alerts
    "daily_brief":    [("brief", "brief")],
    "mining":         [("mining", "mining")],
    "daily_snapshot": [("daily", "daily")],
    "exchange_rate":  [("exchange_rate", "fx_alert")],
}

def _get_sub2_state(chat_id: int) -> set:
    """Return set of active sub2 keys for a chat."""
    subs = db.get_subscriptions(chat_id)
    active = set()
    company_cats = {(s["company"], s["category"]) for s in subs}
    if ("brief", "brief") in company_cats:
        active.add("coin_prices")
        active.add("daily_brief")
    if ("mining", "mining") in company_cats:
        active.add("mining")
    if ("daily", "daily") in company_cats:
        active.add("stock_prices")
        active.add("daily_snapshot")
    if ("exchange_rate", "fx_alert") in company_cats:
        active.add("exchange_rate")
    # Parataxis news: both parataxis and parataxiseth news subscribed
    if ("parataxis", "news") in company_cats and ("parataxiseth", "news") in company_cats:
        active.add("parataxis_news")
    # Competitor news: bitmax + bitplanet news subscribed
    if ("bitmax", "news") in company_cats and ("bitplanet", "news") in company_cats:
        active.add("competitor_news")
    return active



# ── /subscribe ────────────────────────────────────────────────────────────────

async def cmd_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show persistent checkbox-style subscription menu."""
    user    = update.effective_user
    chat_id = update.effective_chat.id
    lang    = db.get_lang(chat_id)

    if not _is_admin(user.id) and not db.is_approved(chat_id):
        await update.message.reply_text("🔒 Access restricted.")
        return

    state = _get_sub2_state(chat_id)
    prompt = (
        "🔔 <b>구독 관리</b>\n\n구독 항목을 선택하거나 해제하세요:"
        if lang == "ko" else
        "🔔 <b>Subscription Manager</b>\n\nTap to toggle subscriptions on or off:"
    )
    await update.message.reply_text(
        prompt,
        reply_markup=kb_subscribe_persistent(lang, state),
        parse_mode=ParseMode.HTML,
    )

# ── /watch ────────────────────────────────────────────────────────────────────

async def cmd_watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    chat_id = update.effective_chat.id
    lang    = db.get_lang(chat_id)

    if not _is_admin(user.id) and not db.is_approved(chat_id):
        await update.message.reply_text("🔒 Access restricted.")
        return

    args = ctx.args or []
    arg  = args[0].lower() if args else ""
    db.log_event("watch", user.id, user.username, chat_id, arg or "menu")

    is_first = not db.has_any_subscription(chat_id)

    if arg in ("news", "기사"):
        for company in _ALL_COMPANIES:
            db.subscribe(chat_id, company, "news")
        msg = (
            "기사 알림이 모든 회사에 대해 활성화되었습니다. ✅"
            if lang == "ko" else
            "Subscribed to <b>News</b> alerts for all companies. ✅"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        if is_first:
            ctx.application.create_task(_seed_dedup_tables())

    elif arg in ("disclosures", "disclosure", "공시"):
        for company in _DART_COMPANIES:
            db.subscribe(chat_id, company, "disclosures")
        msg = (
            "공시 알림이 활성화되었습니다. ✅"
            if lang == "ko" else
            "Subscribed to <b>Disclosures</b> alerts. ✅"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
        if is_first:
            ctx.application.create_task(_seed_dedup_tables())

    elif arg in ("brief", "브리프"):
        db.subscribe(chat_id, "brief", "brief")
        msg = (
            "데일리 마켓 브리프 구독이 활성화되었습니다. 매일 오전 10시에 받으실 수 있습니다. ✅"
            if lang == "ko" else
            "Subscribed to the <b>Daily Market Brief</b>. You'll receive it every day at 10:00 KST. ✅"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

    elif arg in ("mining", "채굴"):
        db.subscribe(chat_id, "mining", "mining")
        msg = (
            "채굴 현황 알림이 활성화되었습니다. ✅"
            if lang == "ko" else
            "Subscribed to <b>Mining Updates</b>. ✅"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

    elif arg in ("daily", "데일리"):
        db.subscribe(chat_id, "daily", "daily")
        msg = (
            "데일리 스냅샷 구독이 활성화되었습니다. 매일 오전 9시에 받으실 수 있습니다. ✅"
            if lang == "ko" else
            "Subscribed to the <b>Daily Snapshot</b>. You'll receive it every day at 9:00 KST. ✅"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

    else:
        msg = (
            "사용법: /watch news | disclosures | brief | mining | daily"
            if lang == "ko" else
            "Usage: /watch news | disclosures | brief | mining | daily"
        )
        await update.message.reply_text(msg)


# ── /unwatch ──────────────────────────────────────────────────────────────────

async def cmd_unwatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    chat_id = update.effective_chat.id
    lang    = db.get_lang(chat_id)
    args    = ctx.args or []
    arg     = args[0].lower() if args else ""
    db.log_event("unwatch", user.id, user.username, chat_id, arg or "menu")

    if arg in ("news", "기사"):
        db.unsubscribe(chat_id, category="news")
        msg = "기사 알림이 해제되었습니다. 🔕" if lang == "ko" else "Unsubscribed from <b>News</b> alerts. 🔕"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    elif arg in ("disclosures", "disclosure", "공시"):
        db.unsubscribe(chat_id, category="disclosures")
        msg = "공시 알림이 해제되었습니다. 🔕" if lang == "ko" else "Unsubscribed from <b>Disclosures</b> alerts. 🔕"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    elif arg in ("brief", "브리프"):
        db.unsubscribe(chat_id, company="brief", category="brief")
        msg = (
            "데일리 마켓 브리프 구독이 해제되었습니다. 🔕"
            if lang == "ko" else
            "Unsubscribed from the <b>Daily Market Brief</b>. 🔕"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    elif arg in ("mining", "채굴"):
        db.unsubscribe(chat_id, company="mining", category="mining")
        msg = (
            "채굴 현황 알림이 해제되었습니다. 🔕"
            if lang == "ko" else
            "Unsubscribed from <b>Mining Updates</b>. 🔕"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    elif arg in ("daily", "데일리"):
        db.unsubscribe(chat_id, company="daily", category="daily")
        msg = (
            "데일리 스냅샷 구독이 해제되었습니다. 🔕"
            if lang == "ko" else
            "Unsubscribed from <b>Daily Snapshot</b>. 🔕"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    elif arg in ("all", "전체"):
        db.unsubscribe(chat_id)
        msg = "모든 알림이 해제되었습니다. 🔕" if lang == "ko" else "Unsubscribed from all alerts. 🔕"
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    else:
        prompt = "취소할 카테고리를 선택하세요:" if lang == "ko" else "Select what to unsubscribe from:"
        await update.message.reply_text(
            prompt,
            reply_markup=kb_unwatch_categories(lang),
            parse_mode=ParseMode.HTML,
        )


# ── /status ───────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lang    = db.get_lang(chat_id)
    subs    = db.get_subscriptions(chat_id)

    if not subs:
        msg = "알림 구독 없음." if lang == "ko" else "Not subscribed to any alerts."
        await update.message.reply_text(msg)
        return

    by_company: dict[str, list[str]] = {}
    for s in subs:
        by_company.setdefault(s["company"], []).append(s["category"])

    lines = ["<b>구독 상태</b>" if lang == "ko" else "<b>Subscription Status</b>", ""]
    for company in _ALL_COMPANIES:
        cats = by_company.get(company)
        if cats:
            label    = _company_label(company, lang)
            cats_str = ", ".join(sorted(cats))
            lines.append(f"✅ <b>{label}</b>: {cats_str}")
    if by_company.get("brief"):
        brief_label = "데일리 마켓 브리프 (매일 오전 10시)" if lang == "ko" else "Daily Market Brief (10:00 KST daily)"
        lines.append(f"✅ <b>{brief_label}</b>")
    if by_company.get("mining"):
        mining_label = "채굴 현황 알림" if lang == "ko" else "Mining Updates"
        lines.append(f"✅ <b>{mining_label}</b>")
    if by_company.get("daily"):
        daily_label = "데일리 스냅샷 (매일 오전 9시)" if lang == "ko" else "Daily Snapshot (9:00 KST daily)"
        lines.append(f"✅ <b>{daily_label}</b>")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ── /audit (admin only) ───────────────────────────────────────────────────────

async def cmd_audit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id if update.effective_user else None):
        await update.message.reply_text("Command not recognized.")
        return
    rows = db.get_recent_audit(20)
    if not rows:
        await update.message.reply_text("No audit records yet.")
        return
    lines = ["<b>🔍 Audit Log (last 20)</b>\n"]
    for r in rows:
        who = f"@{r['username']}" if r["username"] else str(r["user_id"])
        ps  = f"  <code>{(r['payload'] or '')[:60]}</code>" if r["payload"] else ""
        lines.append(f"<code>{r['timestamp']}</code> | {who}\n  <b>{r['event_type']}</b>{ps}")
    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:4000] + "\n…(truncated)"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


# ── /users (admin only) ───────────────────────────────────────────────────────

async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _is_admin(update.effective_user.id if update.effective_user else None):
        await update.message.reply_text("Command not recognized.")
        return
    rows = db.get_recent_users(20)
    if not rows:
        await update.message.reply_text("No users recorded yet.")
        return
    lines = ["<b>👥 Recent Users (last 20)</b>\n"]
    for i, r in enumerate(rows, 1):
        who = f"@{r['username']}" if r["username"] else f"id:{r['user_id']}"
        lines.append(
            f"{i}. {who}  (<code>{r['user_id']}</code>)\n"
            f"   Last seen: <code>{r['last_seen']}</code>"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ── /subs (admin only) ────────────────────────────────────────────────────────

async def cmd_subs(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Show all active subscriptions grouped by chat."""
    if not _is_admin(update.effective_user.id if update.effective_user else None):
        await update.message.reply_text("Command not recognized.")
        return

    rows = db.get_all_subscriptions()
    if not rows:
        await update.message.reply_text("No active subscriptions.")
        return

    # Group by chat_id
    from collections import defaultdict
    by_chat = defaultdict(list)
    for r in rows:
        by_chat[r["chat_id"]].append(f"{r['company']}:{r['category']}")

    lines = ["<b>📋 Active Subscriptions</b>\n"]
    for i, (chat_id, subs) in enumerate(by_chat.items(), 1):
        lines.append(f"{i}. <code>{chat_id}</code>\n   {', '.join(subs)}")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ── /announcement (admin only) ───────────────────────────────────────────────

async def cmd_announcement(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id if user else None):
        await update.message.reply_text("Command not recognized.")
        return

    # Parse raw text to preserve newlines — ctx.args splits on all whitespace
    raw   = (update.message.text or "").strip()
    parts = raw.split(None, 1)          # split on first whitespace run
    msg   = parts[1] if len(parts) > 1 else ""
    if not msg:
        await update.message.reply_text("Usage: /announcement <message>")
        return

    text = "📢 <b>Bot Announcement</b>\n\n" + msg
    chat_ids = db.get_approved_chats()
    sent = 0
    for chat_id in chat_ids:
        try:
            await ctx.bot.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML)
            sent += 1
        except Exception as exc:
            log.warning("[announcement] failed to send to %s: %s", chat_id, exc)

    await update.message.reply_text(f"✅ Announcement sent to {sent}/{len(chat_ids)} chats.")


# ── Callback dispatcher ───────────────────────────────────────────────────────


# ── Exchange Rate (SMBS) ───────────────────────────────────────────────────────

async def _fetch_usd_krw() -> dict | None:
    """
    Fetch USD/KRW rate from Korea Eximbank public exchange-rate API.
    Uses KFTC/Seoul Money Brokerage reference rate when available.
    Falls back to the SMBS page scrape only if the API is unavailable.
    """
    import os as _os
    import re as _re
    from datetime import datetime as _datetime, timedelta as _timedelta
    from zoneinfo import ZoneInfo as _ZoneInfo

    import httpx as _httpx

    def _row_get(row: dict, key: str, default=""):
        """Eximbank docs show uppercase keys, actual JSON often uses lowercase."""
        return row.get(key) or row.get(key.lower()) or row.get(key.upper()) or default

    def _parse_rate(raw) -> float | None:
        try:
            rate = float(str(raw).strip().replace(",", ""))
        except (TypeError, ValueError):
            return None
        return rate if 900 < rate < 2000 else None

    # Primary: Korea Eximbank / public data API
    authkey = (
        _os.environ.get("EXIMBANK_API_KEY")
        or _os.environ.get("KOREAEXIM_API_KEY")
        or _os.environ.get("KOREA_EXIMBANK_API_KEY")
        or ""
    ).strip()

    if authkey:
        try:
            base_url = "https://oapi.koreaexim.go.kr/site/program/financial/exchangeJSON"
            today = _datetime.now(_ZoneInfo("Asia/Seoul")).date()

            async with _httpx.AsyncClient(timeout=10) as client:
                for days_back in range(0, 10):
                    search_date = (today - _timedelta(days=days_back)).strftime("%Y%m%d")
                    r = await client.get(
                        base_url,
                        params={
                            "authkey": authkey,
                            "searchdate": search_date,
                            "data": "AP01",
                        },
                    )
                    r.raise_for_status()
                    data = r.json()

                    if not data:
                        log.warning("[exchange] Korea Eximbank returned empty data for %s", search_date)
                        continue

                    if not isinstance(data, list):
                        log.warning("[exchange] Korea Eximbank returned non-list response for %s: %s", search_date, str(data)[:300])
                        continue

                    first = data[0] if data else {}
                    if isinstance(first, dict):
                        result_code = _row_get(first, "result", "")
                        if str(result_code) in {"2", "3", "4"}:
                            log.warning("[exchange] Korea Eximbank returned result=%s for %s", result_code, search_date)
                            continue

                    for row in data:
                        if not isinstance(row, dict):
                            continue
                        cur_unit = str(_row_get(row, "cur_unit", "")).strip().upper()
                        if cur_unit != "USD":
                            continue

                        # Kyungah nim noted the API uses SMBS/Korea Foreign Exchange Brokerage data.
                        # Use KFTC_DEAL_BAS_R first because that is the Seoul Money Brokerage reference rate.
                        rate_raw = _row_get(row, "kftc_deal_bas_r", "") or _row_get(row, "deal_bas_r", "")
                        rate = _parse_rate(rate_raw)
                        if rate is not None:
                            return {
                                "rate": rate,
                                "source": f"Korea Eximbank API / SMBS KFTC ({search_date})",
                            }

                    log.warning("[exchange] Korea Eximbank returned data for %s but no valid USD row.", search_date)
        except Exception as e:
            log.warning("[exchange] Korea Eximbank API failed: %s", e)
    else:
        log.warning("[exchange] EXIMBANK_API_KEY is not set; trying SMBS fallback.")

    # Fallback: SMBS page scrape
    try:
        async with _httpx.AsyncClient(timeout=10, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        }) as client:
            r = await client.get("http://www.smbs.biz/ExRate/TodayExRate.jsp")
        if r.status_code == 200:
            usd_pos = r.text.upper().find("USD")
            search_area = r.text[usd_pos:usd_pos + 3000] if usd_pos >= 0 else r.text
            matches = _re.findall(r"\d{1,3}(?:,\d{3})+\.\d{2}|\d{3,4}\.\d{2}", search_area)
            for raw in matches:
                rate = _parse_rate(raw)
                if rate is not None:
                    return {"rate": rate, "source": "SMBS fallback"}
            log.warning("[exchange] SMBS page loaded but no valid USD/KRW rate was parsed.")
        else:
            log.warning("[exchange] SMBS returned status %s", r.status_code)
    except Exception as e:
        log.warning("[exchange] SMBS fallback failed: %s", e)

    return None

async def cmd_fx(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    chat_id = update.effective_chat.id
    lang    = db.get_lang(chat_id)

    if not _is_admin(user.id if user else None) and not db.is_approved(chat_id):
        await update.message.reply_text("🔒 Access restricted.")
        return

    db.log_event("fx", user.id if user else None, user.username if user else None, chat_id)

    loading = "⏳ 환율 불러오는 중…" if lang == "ko" else "⏳ Fetching exchange rate…"
    loading_msg = await update.message.reply_text(loading)

    result = await _fetch_usd_krw()
    if result:
        rate    = result["rate"]
        source  = result["source"]
        krw_per = round(1000 / rate, 4)
        if lang == "ko":
            text = (
                f"💱 <b>USD/KRW 환율</b>\n\n"
                f"• 1 USD = <b>₩{rate:,.2f}</b>\n"
                f"• 1,000 KRW = <b>${krw_per:.4f}</b>\n\n"
                f"<i>출처: {source}</i>"
            )
        else:
            text = (
                f"💱 <b>USD/KRW Exchange Rate</b>\n\n"
                f"• 1 USD = <b>₩{rate:,.2f}</b>\n"
                f"• 1,000 KRW = <b>${krw_per:.4f}</b>\n\n"
                f"<i>Source: {source}</i>"
            )
    else:
        text = "⚠️ 환율을 가져오지 못했습니다." if lang == "ko" else "⚠️ Could not fetch exchange rate."

    await loading_msg.edit_text(text, parse_mode=ParseMode.HTML)

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    data    = query.data or ""
    user    = update.effective_user
    chat_id = update.effective_chat.id
    lang    = db.get_lang(chat_id)

    # ── Admin approval ──────────────────────────────────────────────────
    if data.startswith("approve:") or data.startswith("deny:"):
        if not _is_admin(user.id):
            return
        action, target_str = data.split(":", 1)
        target_id = int(target_str)

        if action == "approve":
            db.approve_chat(target_id)
            db.subscribe_default(target_id)
            await query.edit_message_text(
                f"✅ Approved chat <code>{target_id}</code>.", parse_mode=ParseMode.HTML
            )
            # Seed dedup so the newly approved chat doesn't get spammed
            try:
                await _seed_dedup_tables()
            except Exception as e:
                log.warning("Seed failed after approval: %s", e)
            try:
                tl  = db.get_lang(target_id)
                msg = (
                    "✅ 접근이 승인되었습니다. /start 를 눌러 시작하세요."
                    if tl == "ko" else
                    "✅ Access approved. Press /start to begin."
                )
                await ctx.bot.send_message(chat_id=target_id, text=msg)
            except Exception as e:
                log.warning("Could not notify approved user %d: %s", target_id, e)
        else:
            db.deny_chat(target_id)
            await query.edit_message_text(
                f"❌ Denied chat <code>{target_id}</code>.", parse_mode=ParseMode.HTML
            )
        return

    # ── Access gate ─────────────────────────────────────────────────────
    if not _is_admin(user.id) and not db.is_approved(chat_id):
        await query.edit_message_text("🔒 Access restricted.")
        return

    # ── Language selection ──────────────────────────────────────────────
    if data.startswith("lang:"):
        selected_lang = data.split(":")[1]
        db.set_lang(chat_id, selected_lang)
        db.log_event("click", user.id, user.username, chat_id, data)
        if selected_lang == "ko":
            welcome_text = (
                "✅ 언어가 <b>한국어</b>로 설정되었습니다.\n\n"
                "🤖 <b>파라택시스 패밀리 봇으로 할 수 있는 것:</b>\n"
                "• 📊 /subscribe — 데일리 업데이트 구독 (BTC/ETH 가격, 주식, 뉴스)\n"
                "• 📅 /daily — 가격, 주식 &amp; 채굴 현황 데일리 스냅샷\n"
                "• 📸 /brief — BTC &amp; ETH 라이브 대시보드 스크린샷\n"
                "• ⛏️ /mining — 현재 채굴 현황\n"
                "• 🌐 /t — 한국어↔영어 자동 번역\n"
                "• 📋 /kakaoexport — 주주 단체 카카오톡 요약 확인\n\n"
                "아래 메뉴에서 선택하세요:"
            )
        else:
            welcome_text = (
                "✅ Language set to <b>English</b>.\n\n"
                "🤖 <b>What you can do with this bot:</b>\n"
                "• 📊 /subscribe — Subscribe to daily updates (BTC/ETH prices, stock prices, news feeds)\n"
                "• 📅 /daily — Daily snapshot of prices, stocks &amp; mining stats\n"
                "• 📸 /brief — Live BTC &amp; ETH dashboard screenshots\n"
                "• ⛏️ /mining — Current mining stats\n"
                "• 🌐 /t — Translate Korean↔English automatically\n"
                "• 📋 /kakaoexport — Summary of the Kakao shareholder groupchat\n\n"
                "Select from the menu below:"
            )
        await query.edit_message_text(
            welcome_text,
            reply_markup=kb_main(selected_lang),
            parse_mode=ParseMode.HTML,
        )

    # ── Navigation ──────────────────────────────────────────────────────
    elif data == "nav:home":
        db.log_event("click", user.id, user.username, chat_id, data)
        await query.edit_message_text(
            "👋 <b>Welcome to the Parataxis Family Bot</b>\n\n"
            "🤖 <b>What you can do with this bot:</b>\n"
            "• 📊 /subscribe — Subscribe to daily updates (BTC/ETH prices, stock prices, news feeds)\n"
            "• 📅 /daily — Daily snapshot of prices, stocks &amp; mining stats\n"
            "• 📸 /brief — Live BTC &amp; ETH dashboard screenshots\n"
            "• ⛏️ /mining — Current mining stats\n"
            "• 🌐 /t — Translate Korean↔English automatically\n"
            "• 📋 /kakaoexport — Summary of the Kakao shareholder groupchat\n\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🤖 <b>이 봇으로 할 수 있는 것:</b>\n"
            "• 📊 /subscribe — 데일리 업데이트 구독 (BTC/ETH 가격, 주식, 뉴스)\n"
            "• 📅 /daily — 가격, 주식 &amp; 채굴 현황 데일리 스냅샷\n"
            "• 📸 /brief — BTC &amp; ETH 라이브 대시보드 스크린샷\n"
            "• ⛏️ /mining — 현재 채굴 현황\n"
            "• 🌐 /t — 한국어↔영어 자동 번역\n"
            "• 📋 /kakaoexport — 주주 단체 카카오톡 요약 확인\n\n"
            "Select your preferred language / 언어를 선택해 주세요:",
            reply_markup=kb_language(),
            parse_mode=ParseMode.HTML,
        )

    elif data == "nav:main":
        db.log_event("click", user.id, user.username, chat_id, data)
        await query.edit_message_text(
            _main_prompt(lang), reply_markup=kb_main(lang), parse_mode=ParseMode.HTML,
        )

    elif data.startswith("nav:back_to_cat:"):
        company = data.split(":")[2]
        db.log_event("click", user.id, user.username, chat_id, data)
        label  = _company_label(company, lang)
        prompt = (
            f"<b>{label}</b> 카테고리를 선택하세요:"
            if lang == "ko" else
            f"Select category for <b>{label}</b>:"
        )
        await query.edit_message_text(
            prompt, reply_markup=kb_category(lang, company), parse_mode=ParseMode.HTML,
        )

    # ── Price ────────────────────────────────────────────────────────────
    elif data == "menu:price":
        db.log_event("click", user.id, user.username, chat_id, data)
        prompt = "코인을 선택하세요:" if lang == "ko" else "Select a coin:"
        await query.edit_message_text(
            prompt, reply_markup=kb_price(lang), parse_mode=ParseMode.HTML,
        )

    elif data.startswith("price:stock:"):
        # Stock price handler (e.g. Parataxis Korea KOSDAQ 288330)
        ticker = data.split(":")[2]
        db.log_event("click", user.id, user.username, chat_id, data)
        await query.edit_message_text("⏳ Fetching stock price…")
        result = await get_stock_price_krw(ticker)
        await query.edit_message_text(
            fmt_stock_price(result, lang),
            reply_markup=kb_after_price(lang),
            parse_mode=ParseMode.HTML,
        )

    elif data.startswith("price:"):
        coin = data.split(":")[1]
        db.log_event("click", user.id, user.username, chat_id, data)
        await query.edit_message_text("⏳ Fetching price…")
        result = await get_price(coin, lang)
        await query.edit_message_text(
            fmt_price(result, lang),
            reply_markup=kb_after_price(lang),
            parse_mode=ParseMode.HTML,
        )

    # ── Company selection — fetch immediately ───────────────────────────
    elif data.startswith("company:"):
        company = data.split(":")[1]
        db.log_event("click", user.id, user.username, chat_id, data)
        label = _company_label(company, lang)
        loading = f"⏳ {label} 데이터 불러오는 중…" if lang == "ko" else f"⏳ Loading {label}…"
        await query.edit_message_text(loading)

        # Fetch news and disclosures in parallel
        import asyncio as _asyncio
        news_task = get_news(company, no_age_limit=True)
        disc_task = get_disclosures(company) if company in _DART_COMPANIES else None

        if disc_task:
            news_items, disc_items = await _asyncio.gather(news_task, disc_task, return_exceptions=True)
            if isinstance(news_items, Exception): news_items = []
            if isinstance(disc_items, Exception): disc_items = []
        else:
            news_items = await news_task
            disc_items = []

        # Build combined message
        sections = []
        if disc_items and not (len(disc_items) == 1 and "error" in disc_items[0]):
            sections.append(fmt_disclosures(disc_items, lang))
        if news_items:
            sections.append(fmt_news(news_items, lang))
        if not sections:
            text = f"<b>{label}</b>\n\n" + (
                "📭 데이터를 찾을 수 없습니다." if lang == "ko" else "📭 No data found."
            )
        else:
            text = f"<b>📁 {label}</b>\n\n" + "\n\n".join(sections)

        await query.edit_message_text(
            text,
            reply_markup=kb_after_result(lang, company),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    # ── Category selection ──────────────────────────────────────────────
    elif data.startswith("cat:"):
        parts    = data.split(":")
        category = parts[1]
        company  = parts[2]
        db.log_event("click", user.id, user.username, chat_id, data)

        if category == "search":
            ctx.user_data["pending_search"] = {"company": company}
            prompt = (
                f"🔍 검색어를 입력하세요 (<b>{_company_label(company, lang)}</b>):"
                if lang == "ko" else
                f"🔍 Type your search query for <b>{_company_label(company, lang)}</b>:"
            )
            await query.edit_message_text(prompt, parse_mode=ParseMode.HTML)
            return

        await query.edit_message_text("⏳ Fetching…")
        text = await _fetch_text(company, category, lang)
        await query.edit_message_text(
            text,
            reply_markup=kb_after_result(lang, company),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    # ── Subscribe menu ─────────────────────────────────────────────────
    elif data == "menu:logs":
        from sheets import SHEET_URL, WATCHLIST_SHEET_URL
        wl = WATCHLIST_SHEET_URL
        kk = SHEET_URL
        if lang == "ko":
            msg = "<b>📋 로그 & 기록</b>\n\n📰 <a href='" + wl + "'>뉴스 & 공시 워치리스트</a>\n💬 <a href='" + kk + "'>카카오 미팅 로그</a>"
        else:
            msg = "<b>📋 Logs & Records</b>\n\n📰 <a href='" + wl + "'>News & Disclosure Watchlist</a>\n💬 <a href='" + kk + "'>Kakao Meeting Log</a>"
        nav = kb_after_result(lang, "parataxis")
        await query.edit_message_text(msg, reply_markup=nav, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    elif data == "menu:exchange_rate":
        db.log_event("click", user.id, user.username, chat_id, data)
        loading = "⏳ 환율 불러오는 중…" if lang == "ko" else "⏳ Fetching exchange rate…"
        await query.edit_message_text(loading)
        result = await _fetch_usd_krw()
        if result:
            rate     = result["rate"]
            source   = result["source"]
            krw_per  = round(1000 / rate, 4)
            if lang == "ko":
                text = (
                    f"💱 <b>USD/KRW 환율</b>\n\n"
                    f"• 1 USD = <b>₩{rate:,.2f}</b>\n"
                    f"• 1,000 KRW = <b>${krw_per:.4f}</b>\n\n"
                    f"<i>출처: {source}</i>"
                )
            else:
                text = (
                    f"💱 <b>USD/KRW Exchange Rate</b>\n\n"
                    f"• 1 USD = <b>₩{rate:,.2f}</b>\n"
                    f"• 1,000 KRW = <b>${krw_per:.4f}</b>\n\n"
                    f"<i>Source: {source}</i>"
                )
        else:
            text = "⚠️ 환율을 가져오지 못했습니다." if lang == "ko" else "⚠️ Could not fetch exchange rate."
        from telegram import InlineKeyboardMarkup, InlineKeyboardButton as IKB
        nav = InlineKeyboardMarkup([[
            IKB("⬅️ 뒤로" if lang == "ko" else "⬅️ Back", callback_data="nav:main"),
            IKB("🏠 Home", callback_data="nav:home"),
        ]])
        await query.edit_message_text(text, reply_markup=nav, parse_mode=ParseMode.HTML)

    elif data == "menu:translate_help":
        nav = kb_after_result(lang, "parataxis")
        await query.edit_message_text(_T_HELP, reply_markup=nav, parse_mode=ParseMode.HTML)

    # ── sub2: persistent toggle subscriptions ──────────────────────────────────
    elif data.startswith("sub2:"):
        key = data.split(":")[1]
        db.log_event("click", user.id, user.username, chat_id, data)
        state = _get_sub2_state(chat_id)
        is_first = not db.has_any_subscription(chat_id)

        if key == "coin_prices":
            if "coin_prices" in state:
                db.unsubscribe(chat_id, company="brief", category="brief")
            else:
                db.subscribe(chat_id, "brief", "brief")
                if is_first:
                    ctx.application.create_task(_seed_dedup_tables())

        elif key == "stock_prices":
            if "stock_prices" in state:
                db.unsubscribe(chat_id, company="daily", category="daily")
            else:
                db.subscribe(chat_id, "daily", "daily")

        elif key == "daily_brief":
            if "daily_brief" in state:
                db.unsubscribe(chat_id, company="brief", category="brief")
            else:
                db.subscribe(chat_id, "brief", "brief")
                if is_first:
                    ctx.application.create_task(_seed_dedup_tables())

        elif key == "mining":
            if "mining" in state:
                db.unsubscribe(chat_id, company="mining", category="mining")
            else:
                db.subscribe(chat_id, "mining", "mining")

        elif key == "daily_snapshot":
            if "daily_snapshot" in state:
                db.unsubscribe(chat_id, company="daily", category="daily")
            else:
                db.subscribe(chat_id, "daily", "daily")

        elif key == "exchange_rate":
            if "exchange_rate" in state:
                db.unsubscribe(chat_id, company="exchange_rate", category="fx_alert")
            else:
                db.subscribe(chat_id, "exchange_rate", "fx_alert")

        elif key == "parataxis_news":
            # Parataxis news feeds = parataxis + parataxiseth news + disclosures
            if "parataxis_news" in state:
                for co in ("parataxis", "parataxiseth"):
                    db.unsubscribe(chat_id, company=co, category="news")
                    db.unsubscribe(chat_id, company=co, category="disclosures")
            else:
                for co in ("parataxis", "parataxiseth"):
                    db.subscribe(chat_id, co, "news")
                    db.subscribe(chat_id, co, "disclosures")
                if is_first:
                    ctx.application.create_task(_seed_dedup_tables())

        elif key == "competitor_news":
            # Competitor news = bitmax + bitplanet + microstrategy news + disclosures
            if "competitor_news" in state:
                for co in ("bitmax", "bitplanet"):
                    db.unsubscribe(chat_id, company=co, category="news")
                    db.unsubscribe(chat_id, company=co, category="disclosures")
                db.unsubscribe(chat_id, company="microstrategy", category="news")
            else:
                for co in ("bitmax", "bitplanet"):
                    db.subscribe(chat_id, co, "news")
                    db.subscribe(chat_id, co, "disclosures")
                db.subscribe(chat_id, "microstrategy", "news")
                if is_first:
                    ctx.application.create_task(_seed_dedup_tables())

        # Refresh the menu with updated state
        new_state = _get_sub2_state(chat_id)
        prompt = (
            "🔔 <b>구독 관리</b>\n\n구독 항목을 선택하거나 해제하세요:"
            if lang == "ko" else
            "🔔 <b>Subscription Manager</b>\n\nTap to toggle subscriptions on or off:"
        )
        await query.edit_message_text(
            prompt,
            reply_markup=kb_subscribe_persistent(lang, new_state),
            parse_mode=ParseMode.HTML,
        )

    elif data == "menu:subscribe":
        state = _get_sub2_state(chat_id)
        prompt = (
            "🔔 <b>구독 관리</b>\n\n구독 항목을 선택하거나 해제하세요:"
            if lang == "ko" else
            "🔔 <b>Subscription Manager</b>\n\nTap to toggle subscriptions on or off:"
        )
        await query.edit_message_text(
            prompt, reply_markup=kb_subscribe_persistent(lang, state), parse_mode=ParseMode.HTML,
        )

    # ── Subscribe via menu buttons ──────────────────────────────────────
    elif data.startswith("sub:"):
        topic    = data.split(":", 1)[1]
        is_first = not db.has_any_subscription(chat_id)
        db.log_event("click", user.id, user.username, chat_id, data)

        if topic == "all":
            db.subscribe_default(chat_id)
            db.subscribe(chat_id, "brief",  "brief")
            db.subscribe(chat_id, "mining", "mining")
            db.subscribe(chat_id, "daily",  "daily")
            msg = "모든 항목 구독이 활성화되었습니다. ✅" if lang == "ko" else "Subscribed to everything. ✅"
        elif topic in ("parataxis", "parataxiseth", "bitmax", "bitplanet", "microstrategy"):
            db.subscribe(chat_id, topic, "news")
            if topic not in ("microstrategy",):
                db.subscribe(chat_id, topic, "disclosures")
            label = {"parataxis": "Parataxis Korea", "parataxiseth": "Parataxis Ethereum",
                     "bitmax": "Bitmax", "bitplanet": "Bitplanet", "microstrategy": "Strategy"}[topic]
            msg = f"{label} 알림이 활성화되었습니다. ✅" if lang == "ko" else f"Subscribed to <b>{label}</b> alerts. ✅"
        elif topic == "brief":
            db.subscribe(chat_id, "brief", "brief")
            msg = "가격 업데이트 구독이 활성화되었습니다. ✅" if lang == "ko" else "Subscribed to <b>Price Updates</b> (Daily Market Brief). ✅"
        elif topic == "mining":
            db.subscribe(chat_id, "mining", "mining")
            msg = "채굴 현황 알림이 활성화되었습니다. ✅" if lang == "ko" else "Subscribed to <b>Mining Updates</b>. ✅"
        elif topic == "daily":
            db.subscribe(chat_id, "daily", "daily")
            msg = "데일리 스냅샷 구독이 활성화되었습니다. ✅" if lang == "ko" else "Subscribed to <b>Daily Snapshot</b>. ✅"
        else:
            msg = "알 수 없는 항목입니다." if lang == "ko" else "Unknown subscription topic."

        if is_first and topic in ("all", "parataxis", "parataxiseth", "bitmax", "bitplanet", "microstrategy"):
            await query.edit_message_text("⏳ Setting up alerts…")
            await _seed_dedup_tables()

        await query.edit_message_text(msg, parse_mode=ParseMode.HTML)

    # ── Watch via buttons ───────────────────────────────────────────────
    elif data.startswith("watch:"):
        parts    = data.split(":")
        category = parts[2] if len(parts) > 2 else "all"
        db.log_event("click", user.id, user.username, chat_id, data)
        is_first = not db.has_any_subscription(chat_id)

        if category == "all":
            db.subscribe_default(chat_id)
            msg = "모든 알림이 활성화되었습니다. ✅" if lang == "ko" else "Subscribed to all alerts. ✅"
        elif category == "news":
            for company in _ALL_COMPANIES:
                db.subscribe(chat_id, company, "news")
            msg = "기사 알림이 활성화되었습니다. ✅" if lang == "ko" else "Subscribed to News alerts. ✅"
        else:
            for company in _DART_COMPANIES:
                db.subscribe(chat_id, company, "disclosures")
            msg = "공시 알림이 활성화되었습니다. ✅" if lang == "ko" else "Subscribed to Disclosures alerts. ✅"

        if is_first:
            await query.edit_message_text("⏳ Setting up alerts…")
            await _seed_dedup_tables()

        await query.edit_message_text(msg, parse_mode=ParseMode.HTML)

    # ── Unwatch via buttons ─────────────────────────────────────────────
    elif data.startswith("unwatch:"):
        parts    = data.split(":")
        category = parts[2] if len(parts) > 2 else "all"
        db.log_event("click", user.id, user.username, chat_id, data)

        if category == "all":
            db.unsubscribe(chat_id)
            msg = "모든 알림이 해제되었습니다. 🔕" if lang == "ko" else "Unsubscribed from all alerts. 🔕"
        elif category == "news":
            db.unsubscribe(chat_id, category="news")
            msg = "기사 알림이 해제되었습니다. 🔕" if lang == "ko" else "Unsubscribed from News alerts. 🔕"
        else:
            db.unsubscribe(chat_id, category="disclosures")
            msg = "공시 알림이 해제되었습니다. 🔕" if lang == "ko" else "Unsubscribed from Disclosures alerts. 🔕"

        await query.edit_message_text(msg, parse_mode=ParseMode.HTML)

    else:
        log.warning("Unhandled callback: %s", data)


# ── Free text (search) ────────────────────────────────────────────────────────

async def message_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    chat_id = update.effective_chat.id
    lang    = db.get_lang(chat_id)
    text    = (update.message.text or "").strip()

    if not _is_admin(user.id) and not db.is_approved(chat_id):
        await update.message.reply_text("🔒 Access restricted. Use /start to request access.")
        return

    pending = ctx.user_data.get("pending_search")
    if pending:
        ctx.user_data.pop("pending_search")
        company  = pending["company"]
        db.log_event("search", user.id, user.username, chat_id, f"{company}|{text[:200]}")
        category = _detect_category(text, company) or "news"
        result   = await _fetch_text(company, category, lang)
        await update.message.reply_text(
            result,
            reply_markup=kb_after_result(lang, company),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        return

    company  = _detect_company(text)
    category = _detect_category(text, company)

    if company and category:
        db.log_event("search", user.id, user.username, chat_id, f"{company}|{category}|{text[:200]}")
        result = await _fetch_text(company, category, lang)
        await update.message.reply_text(
            result,
            reply_markup=kb_after_result(lang, company),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
    else:
        hint = (
            "🤔 검색어를 이해하지 못했습니다.\n/start 로 메뉴를 여세요."
            if lang == "ko" else
            "🤔 I couldn't detect a company or category.\nUse /start to open the menu."
        )
        await update.message.reply_text(hint, reply_markup=kb_language(), parse_mode=ParseMode.HTML)


# ── Shared fetch ──────────────────────────────────────────────────────────────

async def _fetch_text(company: str, category: str, lang: str) -> str:
    if category == "disclosures" and company in _DART_COMPANIES:
        items = await get_disclosures(company)
        return fmt_disclosures(items, lang)
    # no_age_limit=True so on-demand fetches always show recent articles
    # regardless of the monitor's age filter
    items = await get_news(company, no_age_limit=True)
    return fmt_news(items, lang)


# ── NLP helpers ───────────────────────────────────────────────────────────────

def _detect_company(text: str) -> str:
    t = text.lower()
    if "파라택시스 이더리움" in t or "parataxiseth" in t or "290560" in t:
        return "parataxiseth"
    if "파라택시스" in t or "parataxis" in t:
        return "parataxis"
    if "비트맥스" in t or "bitmax" in t:
        return "bitmax"
    if "비트플래닛" in t or "bitplanet" in t:
        return "bitplanet"
    if "microstrategy" in t or "mstr" in t or "스트래티지" in t:
        return "microstrategy"
    return ""


def _detect_category(text: str, company: str = "") -> str:
    if company == "microstrategy":
        return "news"
    t = text.lower()
    for kw in ["공시", "disclosure", "dart", "filing", "report"]:
        if kw in t:
            return "disclosures"
    for kw in ["기사", "news", "article", "뉴스", "언론"]:
        if kw in t:
            return "news"
    return ""


# ── /brief ─────────────────────────────────────────────────────────────────────

async def cmd_brief(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    On-demand dashboard screenshot — sends both BTC and ETH tracker screenshots.
    Works regardless of subscription status.
    """
    import asyncio as _asyncio
    user    = update.effective_user
    chat_id = update.effective_chat.id
    lang    = db.get_lang(chat_id)

    if not _is_admin(user.id) and not db.is_approved(chat_id):
        await update.message.reply_text("🔒 Access restricted.")
        return

    db.log_event("brief", user.id, user.username, chat_id)
    wait_msg = "⏳ 대시보드 스크린샷 캡처 중…" if lang == "ko" else "⏳ Capturing dashboard screenshots…"
    sent = await update.message.reply_text(wait_msg)

    # Fetch BTC and ETH screenshots in parallel (each launches its own browser)
    async def _safe_shot(coin):
        try:
            return await take_screenshot_with_timeout(lang, coin)
        except BriefError as exc:
            log.error("cmd_brief %s screenshot error: %s", coin.upper(), exc)
            return None

    import asyncio as _asyncio
    btc_bytes, eth_bytes = await _asyncio.gather(
        _safe_shot("btc"),
        _safe_shot("eth"),
    )

    await sent.delete()

    if not btc_bytes and not eth_bytes:
        err_msg = (
            "⚠️ 스크린샷을 가져오지 못했습니다. 잠시 후 다시 시도해 주세요."
            if lang == "ko" else
            "⚠️ Could not capture dashboard screenshots. Please try again shortly."
        )
        await update.message.reply_text(err_msg)
        return

    if btc_bytes:
        caption = "📊 Bitcoin Dashboard" if lang == "en" else "📊 비트코인 대시보드"
        await update.message.reply_photo(photo=btc_bytes, caption=caption)

    if eth_bytes:
        caption = "📊 Ethereum Dashboard" if lang == "en" else "📊 이더리움 대시보드"
        await update.message.reply_photo(photo=eth_bytes, caption=caption)

# ── /mining ────────────────────────────────────────────────────────────────────

async def cmd_mining(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """On-demand mining stats fetch."""
    user    = update.effective_user
    chat_id = update.effective_chat.id
    lang    = db.get_lang(chat_id)

    if not _is_admin(user.id) and not db.is_approved(chat_id):
        await update.message.reply_text("🔒 Access restricted.")
        return

    db.log_event("mining", user.id, user.username, chat_id)
    wait_msg = "⏳ 채굴 현황 불러오는 중…" if lang == "ko" else "⏳ Fetching mining stats…"
    sent = await update.message.reply_text(wait_msg)

    try:
        stats = await get_mining_stats()
    except LuxorError as exc:
        log.error("cmd_mining error: %s", exc)
        err_msg = (
            "⚠️ 채굴 데이터를 가져오지 못했습니다. 잠시 후 다시 시도해 주세요."
            if lang == "ko" else
            "⚠️ Could not fetch mining stats. Please try again shortly."
        )
        await sent.edit_text(err_msg)
        return

    await sent.edit_text(fmt_mining_stats(stats, lang), parse_mode=ParseMode.HTML)


# ── /daily ─────────────────────────────────────────────────────────────────────

def _fmt_daily_header(
    lang: str,
    date_str: str,
    time_str: str,
    btc: dict | None,
    eth: dict | None,
    stock_pk: dict | None,   # Parataxis Korea 288330
    stock_pe: dict | None,   # Parataxis Ethereum 290560
    stats,                   # MiningStats | None
) -> str:
    """Build the executive summary header for /daily."""

    def fmt_coin(d):
        if d and "error" not in d:
            p = d["price"]
            return f"${p:,.0f}" if d.get("currency","USD") != "KRW" else f"₩{p:,.0f}"
        return "N/A"

    def fmt_stock(d):
        if d and "error" not in d:
            p      = d["price"]
            change = d.get("change", 0)
            pct    = d.get("change_pct", "0")
            arrow  = "▲" if change > 0 else ("▼" if change < 0 else "—")
            return f"₩{p:,.0f}", f"{arrow} ₩{abs(change):,.0f} ({pct}%)"
        return "N/A", ""

    btc_str = fmt_coin(btc)
    eth_str = fmt_coin(eth)
    pk_str, pk_chg  = fmt_stock(stock_pk)
    pe_str, pe_chg  = fmt_stock(stock_pe)

    if stats:
        hr_str      = f"{stats.hashrate_ph:.2f} PH/s"
        workers_str = str(stats.active_workers)
        today_str   = f"{stats.btc_today:.8f} BTC"
        mtd_str     = f"{stats.btc_mtd:.8f} BTC"
    else:
        hr_str = workers_str = today_str = mtd_str = "N/A"

    from datetime import date
    month_start = date.today().replace(day=1).strftime("%b %-d")

    if lang == "ko":
        lines = [
            f"<b>📊 파라택시스 데일리 스냅샷 — {date_str} ({time_str} KST)</b>",
            "",
            "<b>시장</b>",
            f"• BTC: <b>{btc_str}</b>",
            f"• ETH: <b>{eth_str}</b>",
            f"• 파라택시스 코리아 (288330): <b>{pk_str}</b>" + (f"  {pk_chg}" if pk_chg else ""),
            f"• 파라택시스 이더리움 (290560): <b>{pe_str}</b>" + (f"  {pe_chg}" if pe_chg else ""),
            "",
            "<b>채굴</b>",
            f"• 플릿 해시레이트: <b>{hr_str}</b>",
            f"• 활성 워커: <b>{workers_str}</b>",
            f"• 오늘 채굴: <b>{today_str}</b>",
            f"• 이번 달 채굴 ({month_start}~): <b>{mtd_str}</b>",
            "",
            "대시보드 및 상세 지표는 아래를 확인하세요 ↓",
        ]
    else:
        lines = [
            f"<b>📊 Parataxis Daily Snapshot — {date_str} ({time_str} KST)</b>",
            "",
            "<b>Market</b>",
            f"• BTC: <b>{btc_str}</b>",
            f"• ETH: <b>{eth_str}</b>",
            f"• Parataxis Korea (288330): <b>{pk_str}</b>" + (f"  {pk_chg}" if pk_chg else ""),
            f"• Parataxis Ethereum (290560): <b>{pe_str}</b>" + (f"  {pe_chg}" if pe_chg else ""),
            "",
            "<b>Mining</b>",
            f"• Fleet Hashrate: <b>{hr_str}</b>",
            f"• Active Workers: <b>{workers_str}</b>",
            f"• BTC Today: <b>{today_str}</b>",
            f"• BTC MTD (since {month_start}): <b>{mtd_str}</b>",
            "",
            "Dashboard and detailed metrics below ↓",
        ]
    return "\n".join(lines)


async def cmd_daily(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Combined daily snapshot: header + brief graphic + mining stats + stock price."""
    user    = update.effective_user
    chat_id = update.effective_chat.id
    lang    = db.get_lang(chat_id)

    if not _is_admin(user.id) and not db.is_approved(chat_id):
        await update.message.reply_text("🔒 Access restricted.")
        return

    db.log_event("daily", user.id, user.username, chat_id)
    wait_msg = "⏳ 데일리 스냅샷 준비 중…" if lang == "ko" else "⏳ Preparing daily snapshot…"
    sent = await update.message.reply_text(wait_msg)

    import asyncio as _asyncio
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    now      = _dt.now(_ZI("Asia/Seoul"))
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")

    # Fetch all data sources in parallel
    btc_result = eth_result = stock_pk_result = stock_pe_result = stats_result = None
    try:
        btc_result, eth_result, stock_pk_result, stock_pe_result, stats_result = await _asyncio.gather(
            get_price_usd("btc"),
            get_price_usd("eth"),
            get_stock_price_krw(PARATAXIS_TICKER),          # 288330
            get_stock_price_krw("290560"),                   # Parataxis Ethereum
            get_mining_stats(),
            return_exceptions=True
        )
        if isinstance(btc_result,      Exception): btc_result      = None
        if isinstance(eth_result,      Exception): eth_result      = None
        if isinstance(stock_pk_result, Exception): stock_pk_result = None
        if isinstance(stock_pe_result, Exception): stock_pe_result = None
        if isinstance(stats_result,    Exception): stats_result    = None
    except Exception as exc:
        log.error("cmd_daily gather error: %s", exc)

    # 1. Header summary
    header = _fmt_daily_header(
        lang, date_str, time_str,
        btc_result, eth_result, stock_pk_result, stock_pe_result, stats_result
    )
    await sent.edit_text(header, parse_mode=ParseMode.HTML)

    # 2+3. BTC + ETH screenshots in parallel
    async def _safe_daily_shot(coin):
        try:
            return await take_screenshot_with_timeout(lang, coin)
        except BriefError as exc:
            log.error("cmd_daily %s screenshot error: %s", coin.upper(), exc)
            return None

    btc_png, eth_png = await _asyncio.gather(
        _safe_daily_shot("btc"),
        _safe_daily_shot("eth"),
    )

    if btc_png:
        btc_cap = f"📊 Bitcoin Dashboard — {date_str}" if lang == "en" else f"📊 비트코인 대시보드 — {date_str}"
        await update.message.reply_photo(photo=btc_png, caption=btc_cap)
    else:
        err = "⚠️ BTC 스크린샷을 가져오지 못했습니다." if lang == "ko" else "⚠️ Could not capture BTC dashboard."
        await update.message.reply_text(err)

    if eth_png:
        eth_cap = f"📊 Ethereum Dashboard — {date_str}" if lang == "en" else f"📊 이더리움 대시보드 — {date_str}"
        await update.message.reply_photo(photo=eth_png, caption=eth_cap)
    else:
        err = "⚠️ ETH 스크린샷을 가져오지 못했습니다." if lang == "ko" else "⚠️ Could not capture ETH dashboard."
        await update.message.reply_text(err)



# ── /kakao + /kakaoexport ──────────────────────────────────────────────────────

import sheets as _sheets

_KAKAO_ALLOWED = {7205462694, 8168826794, 921350602}  # Tony, David, Jason


async def cmd_kakao(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Log a Kakao meeting note. Authorized users only."""
    user = update.effective_user
    if not user or user.id not in _KAKAO_ALLOWED:
        await update.message.reply_text("Command not recognized.")
        return

    # Preserve exact formatting — split raw text after the command token
    raw   = (update.message.text or "")
    parts = raw.split(None, 1)
    msg   = parts[1] if len(parts) > 1 else ""
    if not msg:
        await update.message.reply_text("Usage: /kakao <message>")
        return

    display = user.username or user.full_name or str(user.id)
    db.kakao_log_add(user.id, display, msg)

    # Fire-and-forget to Google Sheets — failure never affects the bot
    from datetime import datetime
    from zoneinfo import ZoneInfo
    ts = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M KST")
    ctx.application.create_task(_sheets.append_kakao_entry(ts, display, msg))

    await update.message.reply_text("✅ Logged.")


async def cmd_kakaoexport(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Export all kakao log entries. Available to all approved users."""
    entries = db.kakao_log_get_recent(3)
    if not entries:
        await update.message.reply_text("No kakao log entries yet.")
        return

    lines = []
    for e in entries:
        who = e.get("username") or str(e.get("user_id", "unknown"))
        # Format: 3/23/2026 5:18pm  JDragon812 wrote '...'
        # logged_at is already "YYYY-MM-DD HH:MM KST"
        ts_raw = e.get("logged_at", "")
        try:
            from datetime import datetime
            dt = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M KST")
            ts = dt.strftime("%-m/%-d/%Y %-I:%M%p").lower()
            # Capitalise AM/PM
            ts = ts[:-2] + ts[-2:].upper()
        except Exception:
            ts = ts_raw
        text = e.get("message", "")
        lines.append(f"{ts}  {who} wrote '{text}'")

    output = "\n\n".join(lines)

    sheet_link = (
        "\n\n📋 Full log: https://docs.google.com/spreadsheets/d/1oQcNwpGjePKFvUaIyN44tKU04RtQpHBCZNMy1q2scbg"
    )

    # If too long, truncate with ellipsis and always append sheet link
    LIMIT = 3800
    if len(output) > LIMIT:
        output = output[:LIMIT].rsplit("\n", 1)[0] + "\n\n..."
    await update.message.reply_text(output + sheet_link)

# ── /t (translation) ──────────────────────────────────────────────────────────

from openai_translate import TranslateError, translate as _oa_translate, detect_lang as _oa_detect, SUPPORTED_LANGS as _T_LANGS

_T_HELP = (
    "<b>Translation command usage:</b>\n\n"
    "/t &lt;message&gt; — auto-detects English↔Korean and translates to the opposite language\n"
    "/t en &lt;message&gt; — translate to English (explicit override)\n"
    "/t ko &lt;message&gt; — translate to Korean (explicit override)\n"
    "/t help — show this message"
)


async def cmd_t(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Translation command."""
    user    = update.effective_user
    chat_id = update.effective_chat.id

    if not _is_admin(user.id) and not db.is_approved(chat_id):
        await update.message.reply_text("🔒 Access restricted.")
        return

    # Preserve full formatting — read raw text after /t
    raw   = (update.message.text or "").strip()
    # Strip @BotName suffix for group chats e.g. /t@ParataxisBot -> /t
    first_token = raw.split(None, 1)[0].split("@")[0]
    rest        = raw[len(raw.split(None, 1)[0]):].strip() if len(raw.split(None, 1)) > 1 else ""
    body        = rest

    log.info("[cmd_t] raw=%r body=%r has_reply=%s", raw, body, bool(update.message.reply_to_message))

    # ── /t (reply-based) — /t with no body sent as a reply to another message ──
    if not body and update.message.reply_to_message:
        replied_msg  = update.message.reply_to_message
        replied_text = (replied_msg.text or replied_msg.caption or "").strip()
        if not replied_text:
            await update.message.reply_text("The replied-to message has no text to translate.")
            return
        wait = await update.message.reply_text("⏳ Translating…")
        try:
            result = await _oa_translate(replied_text, None)
            await wait.edit_text(result)
        except TranslateError as exc:
            log.error("cmd_t reply translate error: %s", exc)
            await wait.edit_text("⚠️ Translation failed. Please try again shortly.")
        return

    # ── /t help ──
    if not body or body.strip().lower() == "help":
        await update.message.reply_text(_T_HELP, parse_mode=ParseMode.HTML)
        return

    # ── /t status ──
    if body.strip().lower() == "status":
        lang = db.get_t_lang(user.id)
        if lang:
            await update.message.reply_text(f"Your default translation language is: <b>{lang}</b>", parse_mode=ParseMode.HTML)
        else:
            await update.message.reply_text("You have no default translation language set. Use /t set en or /t set ko.")
        return

    # ── /t unset ──
    if body.strip().lower() == "unset":
        db.unset_t_lang(user.id)
        await update.message.reply_text("✅ Default translation language removed.")
        return

    # ── /t set <lang> ──
    set_parts = body.strip().lower().split(None, 1)
    if set_parts[0] == "set":
        if len(set_parts) < 2 or set_parts[1] not in _T_LANGS:
            await update.message.reply_text(f"Supported languages: {', '.join(sorted(_T_LANGS))}\nExample: /t set ko")
            return
        db.set_t_lang(user.id, set_parts[1])
        await update.message.reply_text(f"✅ Default translation language set to: <b>{set_parts[1]}</b>", parse_mode=ParseMode.HTML)
        return

    # ── /t <lang> <message> — explicit override ──
    first_word = body.split(None, 1)[0].lower()
    if first_word in _T_LANGS:
        target_lang = first_word
        msg_parts   = body.split(None, 1)
        text        = msg_parts[1] if len(msg_parts) > 1 else ""
        if not text:
            await update.message.reply_text(f"Usage: /t {target_lang} <message>")
            return
    else:
        # ── /t <message> — use saved default or auto-detect ──
        text = body
        saved = db.get_t_lang(user.id)
        if saved:
            target_lang = saved
        else:
            # Auto-detect handled inside translate call — pass None
            target_lang = None

    # Translate
    wait = await update.message.reply_text("⏳ Translating…")
    try:
        result = await _oa_translate(text, target_lang)  # target_lang=None means auto-detect
        await wait.edit_text(result)
    except TranslateError as exc:
        log.error("cmd_t translate error: %s", exc)
        await wait.edit_text("⚠️ Translation failed. Please try again shortly.")
