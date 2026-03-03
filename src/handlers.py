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
    kb_category, kb_language, kb_main, kb_price,
    kb_unwatch_categories, kb_watch_categories,
)
from news import get_news
from prices import fmt_price, fmt_stock_price, get_price, get_stock_price_krw, PARATAXIS_TICKER

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
        if is_first:
            await _seed_dedup_tables()
        msg = (
            "기사 알림이 모든 회사에 대해 활성화되었습니다. ✅"
            if lang == "ko" else
            "Subscribed to <b>News</b> alerts for all companies. ✅"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

    elif arg in ("disclosures", "disclosure", "공시"):
        for company in _DART_COMPANIES:
            db.subscribe(chat_id, company, "disclosures")
        if is_first:
            await _seed_dedup_tables()
        msg = (
            "공시 알림이 활성화되었습니다. ✅"
            if lang == "ko" else
            "Subscribed to <b>Disclosures</b> alerts. ✅"
        )
        await update.message.reply_text(msg, parse_mode=ParseMode.HTML)

    else:
        prompt = "구독할 카테고리를 선택하세요:" if lang == "ko" else "Select what to subscribe to:"
        await update.message.reply_text(
            prompt,
            reply_markup=kb_watch_categories(lang),
            parse_mode=ParseMode.HTML,
        )


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
