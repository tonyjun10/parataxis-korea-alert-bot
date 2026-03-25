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
    kb_unwatch_categories, kb_watch_categories,
)
from news import get_news
from prices import fmt_price, fmt_stock_price, get_price, get_price_usd, get_stock_price_krw, PARATAXIS_TICKER
from brief import BriefError, take_screenshot_with_timeout
from luxor import LuxorError, fmt_mining_stats, get_mining_stats

log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────────────
ADMIN_USER_ID: int = 7205462694

_DART_COMPANIES = {"parataxis", "bitmax", "bitplanet"}
_ALL_COMPANIES  = ["parataxis", "bitmax", "bitplanet", "microstrategy"]

_COMPANY_LABEL = {
    "parataxis":     {"en": "Parataxis Korea", "ko": "파라택시스 코리아"},
    "bitmax":        {"en": "Bitmax",          "ko": "비트맥스"},
    "bitplanet":     {"en": "Bitplanet",       "ko": "비트플래닛"},
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
    await update.message.reply_text(
        "🌏 <b>Parataxis Korea Alert Bot</b>\n\nPlease select your language:",
        reply_markup=kb_language(),
        parse_mode=ParseMode.HTML,
    )


# ── /help ─────────────────────────────────────────────────────────────────────

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lang = db.get_lang(update.effective_chat.id)
    if lang == "ko":
        text = (
            "<b>📖 도움말</b>\n\n"
            "• /start — 언어 선택\n"
            "• /watch — 알림 구독 (인수 없으면 메뉴)\n"
            "• /watch news — 뉴스 알림 구독\n"
            "• /watch disclosures — 공시 알림 구독\n"
            "• /unwatch — 구독 취소\n"
            "• /status — 구독 상태 확인\n"
            "• /watch brief — 데일리 브리프 구독\n"
            "• /brief — 지금 브리프 보기\n"
            "• /watch mining — 채굴 현황 알림 구독\n"
            "• /mining — 채굴 현황 보기\n"
            "• /watch daily — 데일리 스냅샷 구독\n"
            "• /daily — 데일리 스냅샷 보기\n"
            "• /help — 이 도움말"
        )
    else:
        text = (
            "<b>📖 Help</b>\n\n"
            "• /start — Language selection\n"
            "• /watch — Subscribe to alerts (menu if no argument)\n"
            "• /watch news — Subscribe to news alerts\n"
            "• /watch disclosures — Subscribe to disclosure alerts\n"
            "• /unwatch — Unsubscribe\n"
            "• /status — Check subscription status\n"
            "• /watch brief — Subscribe to daily market brief\n"
            "• /brief — Get the brief right now\n"
            "• /watch mining — Subscribe to mining updates\n"
            "• /mining — Get mining stats right now\n"
            "• /watch daily — Subscribe to daily snapshot\n"
            "• /daily — Get daily snapshot right now\n"
            "• /help — This message"
        )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


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
        label = "English" if selected_lang == "en" else "한국어"
        await query.edit_message_text(
            f"🌐 Language set to <b>{label}</b>.\n\n{_main_prompt(selected_lang)}",
            reply_markup=kb_main(selected_lang),
            parse_mode=ParseMode.HTML,
        )

    # ── Navigation ──────────────────────────────────────────────────────
    elif data == "nav:home":
        db.log_event("click", user.id, user.username, chat_id, data)
        await query.edit_message_text(
            "🌏 <b>Parataxis Korea Alert Bot</b>\n\nPlease select your language:",
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

    # ── Company selection ───────────────────────────────────────────────
    elif data.startswith("company:"):
        company = data.split(":")[1]
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
    elif data == "menu:subscribe":
        prompt = "구독할 항목을 선택하세요:" if lang == "ko" else "Choose what to subscribe to:"
        await query.edit_message_text(
            prompt, reply_markup=kb_subscribe(lang), parse_mode=ParseMode.HTML,
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
        elif topic in ("parataxis", "bitmax", "bitplanet", "microstrategy"):
            db.subscribe(chat_id, topic, "news")
            if topic != "microstrategy":
                db.subscribe(chat_id, topic, "disclosures")
            label = {"parataxis": "Parataxis Korea", "bitmax": "Bitmax",
                     "bitplanet": "Bitplanet", "microstrategy": "Strategy"}[topic]
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

        if is_first and topic in ("all", "parataxis", "bitmax", "bitplanet", "microstrategy"):
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
    items = await get_news(company)
    return fmt_news(items, lang)


# ── NLP helpers ───────────────────────────────────────────────────────────────

def _detect_company(text: str) -> str:
    t = text.lower()
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
    On-demand dashboard screenshot. Sends the correct language dashboard.
    Works regardless of subscription status.
    """
    user    = update.effective_user
    chat_id = update.effective_chat.id
    lang    = db.get_lang(chat_id)

    if not _is_admin(user.id) and not db.is_approved(chat_id):
        await update.message.reply_text("🔒 Access restricted.")
        return

    db.log_event("brief", user.id, user.username, chat_id)
    wait_msg = "⏳ 대시보드 스크린샷 캡처 중…" if lang == "ko" else "⏳ Capturing dashboard screenshot…"
    sent = await update.message.reply_text(wait_msg)

    try:
        png_bytes = await take_screenshot_with_timeout(lang)
    except BriefError as exc:
        log.error("cmd_brief screenshot error: %s", exc)
        err_msg = (
            "⚠️ 스크린샷을 가져오지 못했습니다. 잠시 후 다시 시도해 주세요."
            if lang == "ko" else
            "⚠️ Could not capture the dashboard screenshot. Please try again shortly."
        )
        await sent.edit_text(err_msg)
        return

    await sent.delete()
    caption = (
        "📊 데일리 마켓 대시보드"
        if lang == "ko" else
        "📊 Market Dashboard"
    )
    await update.message.reply_photo(
        photo=png_bytes,
        caption=caption,
    )

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
    stock: dict | None,
    stats,           # MiningStats | None
) -> str:
    """Build the executive summary header for /daily."""

    # BTC price
    if btc and "error" not in btc:
        btc_price = btc["price"]
        if btc.get("currency", "USD") == "KRW":
            btc_str = f"₩{btc_price:,.0f}"
        else:
            btc_str = f"${btc_price:,.0f}"
    else:
        btc_str = "N/A"

    # Stock price
    if stock and "error" not in stock:
        s_price  = stock["price"]
        s_change = stock.get("change", 0)
        s_pct    = stock.get("change_pct", "0")
        arrow    = "▲" if s_change > 0 else ("▼" if s_change < 0 else "—")
        stock_str  = f"₩{s_price:,.0f}"
        change_str = f"{arrow} ₩{abs(s_change):,.0f} ({s_pct}%)"
    else:
        stock_str  = "N/A"
        change_str = ""

    # Mining stats
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
            f"• 파라택시스 코리아: <b>{stock_str}</b>" + (f"  {change_str}" if change_str else ""),
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
            f"• Parataxis Korea: <b>{stock_str}</b>" + (f"  {change_str}" if change_str else ""),
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

    # Fetch all data sources in parallel
    import asyncio as _asyncio
    from datetime import datetime as _dt
    from zoneinfo import ZoneInfo as _ZI
    now      = _dt.now(_ZI("Asia/Seoul"))
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M")

    btc_task   = get_price_usd("btc")
    stock_task = get_stock_price_krw(PARATAXIS_TICKER)
    mining_task = get_mining_stats()

    btc_result = stock_result = stats_result = None
    try:
        btc_result, stock_result, stats_result = await _asyncio.gather(
            btc_task, stock_task, mining_task, return_exceptions=True
        )
        if isinstance(btc_result,   Exception): btc_result   = None
        if isinstance(stock_result, Exception): stock_result = None
        if isinstance(stats_result, Exception): stats_result = None
    except Exception as exc:
        log.error("cmd_daily gather error: %s", exc)

    # 1. Header summary
    header = _fmt_daily_header(lang, date_str, time_str, btc_result, stock_result, stats_result)
    await sent.edit_text(header, parse_mode=ParseMode.HTML)

    # 2. Brief screenshot
    try:
        png_bytes = await take_screenshot_with_timeout(lang)
        caption = (
            f"📊 데일리 마켓 대시보드 — {date_str}"
            if lang == "ko" else
            f"📊 Daily Market Dashboard — {date_str}"
        )
        await update.message.reply_photo(photo=png_bytes, caption=caption)
    except BriefError as exc:
        log.error("cmd_daily brief error: %s", exc)
        err = "⚠️ 스크린샷을 가져오지 못했습니다." if lang == "ko" else "⚠️ Could not capture dashboard screenshot."
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

    # Telegram max message length is 4096 chars; chunk if needed
    LIMIT = 4000
    if len(output) <= LIMIT:
        await update.message.reply_text(output + sheet_link)
    else:
        for i in range(0, len(output), LIMIT):
            await update.message.reply_text(output[i:i + LIMIT])

# ── /t (translation) ──────────────────────────────────────────────────────────

from openai_translate import TranslateError, translate as _oa_translate, SUPPORTED_LANGS as _T_LANGS

_T_HELP = (
    "<b>Translation command usage:</b>\n\n"
    "/t help — show this message\n"
    "/t set en — set default language to English\n"
    "/t set ko — set default language to Korean\n"
    "/t status — show your current default language\n"
    "/t unset — remove your default language\n"
    "/t &lt;message&gt; — translate using your default language\n"
    "/t en &lt;message&gt; — translate to English (override)\n"
    "/t ko &lt;message&gt; — translate to Korean (override)"
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
    parts = raw.split(None, 1)
    body  = parts[1] if len(parts) > 1 else ""

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
        # ── /t <message> — use saved default ──
        target_lang = db.get_t_lang(user.id)
        if not target_lang:
            await update.message.reply_text(
                "No default language set. Use /t set ko or /t ko <message>."
            )
            return
        text = body

    # Translate
    wait = await update.message.reply_text("⏳ Translating…")
    try:
        result = await _oa_translate(text, target_lang)
        await wait.edit_text(result)
    except TranslateError as exc:
        log.error("cmd_t translate error: %s", exc)
        await wait.edit_text("⚠️ Translation failed. Please try again shortly.")
