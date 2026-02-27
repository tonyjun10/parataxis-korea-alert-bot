"""
handlers.py — Command and callback query handlers.
"""

import logging
import os
from telegram import Update
from telegram.ext import ContextTypes
from telegram.constants import ParseMode

from db import (
    get_lang, set_lang,
    subscribe, unsubscribe, is_subscribed, get_subscription,
    log_event, get_recent_audit, get_recent_users,
)
from dart import get_disclosures
from news import get_news
from formatter import fmt_disclosures, fmt_news
from keyboards import (
    kb_language, kb_company, kb_category, kb_after_result,
    kb_watch_categories, kb_unwatch_categories,
)

log = logging.getLogger(__name__)

# ── Admin ──────────────────────────────────────────────────────────────────────
ADMIN_USER_ID: int = 7205462694

# ── Companies that support DART disclosures ────────────────────────────────────
_DART_COMPANIES = {"bitmax", "bitplanet"}

# ── Subscription allow-list (optional env var) ─────────────────────────────────
ALLOWED_CHAT_IDS_RAW = os.environ.get("ALLOWED_CHAT_IDS", "")
ALLOWED_CHAT_IDS = (
    {int(x.strip()) for x in ALLOWED_CHAT_IDS_RAW.split(",") if x.strip()}
    if ALLOWED_CHAT_IDS_RAW else set()
)


def _is_allowed(chat_id: int) -> bool:
    if not ALLOWED_CHAT_IDS:
        return True
    return chat_id in ALLOWED_CHAT_IDS


def _is_admin(user_id: int | None) -> bool:
    return user_id == ADMIN_USER_ID


# ── Category argument parser (bilingual) ──────────────────────────────────────

_CAT_MAP = {
    # English
    "disclosures": "disclosures",
    "disclosure":  "disclosures",
    "news":        "news",
    "all":         "all",
    # Korean
    "공시": "disclosures",
    "기사": "news",
    "전체": "all",
}


def _parse_category(arg: str) -> str | None:
    return _CAT_MAP.get(arg.strip().lower())


# ── /start ─────────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    chat_id = update.effective_chat.id
    log_event("start", user.id, user.username, chat_id)
    await update.message.reply_text(
        "🌏 <b>Parataxis Korea Alert Bot</b>\n\nPlease select your language:",
        reply_markup=kb_language(),
        parse_mode=ParseMode.HTML,
    )


# ── /help ─────────────────────────────────────────────────────────────────────

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lang = get_lang(update.effective_chat.id)
    if lang == "ko":
        text = (
            "<b>📖 도움말</b>\n\n"
            "• /start — 언어 선택\n"
            "• /watch [공시|기사|전체] — 알림 구독\n"
            "• /unwatch [공시|기사|전체] — 구독 취소\n"
            "• /status — 구독 상태 확인\n"
            "• /help — 이 도움말\n\n"
            "인수를 생략하면 버튼 메뉴가 나타납니다."
        )
    else:
        text = (
            "<b>📖 Help</b>\n\n"
            "• /start — Language selection\n"
            "• /watch [disclosures|news|all] — Subscribe to alerts\n"
            "• /unwatch [disclosures|news|all] — Unsubscribe\n"
            "• /status — Check subscription status\n"
            "• /help — This message\n\n"
            "Omit the argument to see a button menu."
        )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ── /watch ────────────────────────────────────────────────────────────────────

async def cmd_watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    chat_id = update.effective_chat.id
    lang    = get_lang(chat_id)

    if not _is_allowed(chat_id):
        await update.message.reply_text("⛔ Not authorised.")
        return

    args = ctx.args or []
    raw  = args[0] if args else ""
    cat  = _parse_category(raw) if raw else None

    log_event("watch", user.id, user.username, chat_id, raw or "menu")

    if not cat:
        prompt = "구독할 카테고리를 선택하세요:" if lang == "ko" else "Select a category to subscribe:"
        await update.message.reply_text(
            prompt,
            reply_markup=kb_watch_categories(lang),
            parse_mode=ParseMode.HTML,
        )
        return

    subscribe(chat_id, category=cat, lang=lang)
    await update.message.reply_text(_watch_confirm(cat, lang), parse_mode=ParseMode.HTML)


def _watch_confirm(cat: str, lang: str) -> str:
    if lang == "ko":
        labels = {
            "disclosures": "공시 알림이 활성화되었습니다. ✅",
            "news":        "기사 알림이 활성화되었습니다. ✅",
            "all":         "모든 카테고리 알림이 활성화되었습니다. ✅",
        }
    else:
        labels = {
            "disclosures": "You are now subscribed to <b>Disclosures</b> alerts. ✅",
            "news":        "You are now subscribed to <b>News</b> alerts. ✅",
            "all":         "You are now subscribed to <b>all</b> alert categories. ✅",
        }
    return labels.get(cat, "Subscribed. ✅")


# ── /unwatch ──────────────────────────────────────────────────────────────────

async def cmd_unwatch(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    chat_id = update.effective_chat.id
    lang    = get_lang(chat_id)
    args    = ctx.args or []
    raw     = args[0] if args else ""
    cat     = _parse_category(raw) if raw else None

    log_event("unwatch", user.id, user.username, chat_id, raw or "menu")

    if not cat:
        prompt = "취소할 카테고리를 선택하세요:" if lang == "ko" else "Select a category to unsubscribe:"
        await update.message.reply_text(
            prompt,
            reply_markup=kb_unwatch_categories(lang),
            parse_mode=ParseMode.HTML,
        )
        return

    unsubscribe(chat_id, category=cat)
    await update.message.reply_text(_unwatch_confirm(cat, lang), parse_mode=ParseMode.HTML)


def _unwatch_confirm(cat: str, lang: str) -> str:
    if lang == "ko":
        labels = {
            "disclosures": "공시 알림이 해제되었습니다. 🔕",
            "news":        "기사 알림이 해제되었습니다. 🔕",
            "all":         "모든 알림이 해제되었습니다. 🔕",
        }
    else:
        labels = {
            "disclosures": "Unsubscribed from <b>Disclosures</b> alerts. 🔕",
            "news":        "Unsubscribed from <b>News</b> alerts. 🔕",
            "all":         "Unsubscribed from all alert categories. 🔕",
        }
    return labels.get(cat, "Unsubscribed. 🔕")


# ── /status ───────────────────────────────────────────────────────────────────

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    lang    = get_lang(chat_id)
    sub     = get_subscription(chat_id)

    def _tick(val: int) -> str:
        return "✅" if val else "❌"

    if not sub["subscribed"]:
        msg = "알림 구독 없음." if lang == "ko" else "Not subscribed to any alerts."
    else:
        if lang == "ko":
            msg = (
                "<b>구독 상태</b>\n"
                f"공시: {_tick(sub['disclosures_enabled'])}\n"
                f"기사: {_tick(sub['news_enabled'])}"
            )
        else:
            msg = (
                "<b>Subscription Status</b>\n"
                f"Disclosures: {_tick(sub['disclosures_enabled'])}\n"
                f"News:        {_tick(sub['news_enabled'])}"
            )
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


# ── /audit (admin only) ───────────────────────────────────────────────────────

async def cmd_audit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id if user else None):
        await update.message.reply_text("Command not recognized.")
        return
    rows = get_recent_audit(20)
    if not rows:
        await update.message.reply_text("No audit records yet.")
        return
    lines = ["<b>🔍 Audit Log (last 20)</b>\n"]
    for r in rows:
        who = f"@{r['username']}" if r["username"] else str(r["user_id"])
        ps  = f"  <code>{(r['payload'] or '')[:60]}</code>" if r["payload"] else ""
        lines.append(
            f"<code>{r['timestamp']}</code> | {who}\n"
            f"  <b>{r['event_type']}</b>{ps}"
        )
    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:4000] + "\n…(truncated)"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


# ── /users (admin only) ───────────────────────────────────────────────────────

async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not _is_admin(user.id if user else None):
        await update.message.reply_text("Command not recognized.")
        return
    rows = get_recent_users(20)
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


# ── Callback query dispatcher ─────────────────────────────────────────────────

async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    data    = query.data
    user    = update.effective_user
    chat_id = update.effective_chat.id
    lang    = get_lang(chat_id)

    # ── Language selection ──────────────────────────────────────────────
    if data.startswith("lang:"):
        lang = data.split(":")[1]
        set_lang(chat_id, lang)
        log_event("click", user.id, user.username, chat_id, data)
        label  = "English" if lang == "en" else "한국어"
        prompt = "Select company:" if lang == "en" else "회사를 선택하세요:"
        await query.edit_message_text(
            f"🌐 Language set to <b>{label}</b>.\n\n{prompt}",
            reply_markup=kb_company(lang),
            parse_mode=ParseMode.HTML,
        )

    # ── Watch/unwatch category buttons ─────────────────────────────────
    elif data.startswith("watch:"):
        cat = data.split(":")[1]
        log_event("click", user.id, user.username, chat_id, data)
        subscribe(chat_id, category=cat, lang=lang)
        await query.edit_message_text(_watch_confirm(cat, lang), parse_mode=ParseMode.HTML)

    elif data.startswith("unwatch:"):
        cat = data.split(":")[1]
        log_event("click", user.id, user.username, chat_id, data)
        unsubscribe(chat_id, category=cat)
        await query.edit_message_text(_unwatch_confirm(cat, lang), parse_mode=ParseMode.HTML)

    # ── Navigation ──────────────────────────────────────────────────────
    elif data == "nav:home":
        log_event("click", user.id, user.username, chat_id, "nav:home")
        await query.edit_message_text(
            "🌏 <b>Parataxis Korea Alert Bot</b>\n\nPlease select your language:",
            reply_markup=kb_language(),
            parse_mode=ParseMode.HTML,
        )

    elif data == "nav:back_to_company":
        log_event("click", user.id, user.username, chat_id, data)
        prompt = "Select company:" if lang == "en" else "회사를 선택하세요:"
        await query.edit_message_text(
            prompt,
            reply_markup=kb_company(lang),
            parse_mode=ParseMode.HTML,
        )

    elif data.startswith("nav:back_to_cat:"):
        log_event("click", user.id, user.username, chat_id, data)
        company = data.split(":")[2]
        label   = _company_label(company, lang)
        prompt  = (
            f"Select category for <b>{label}</b>:"
            if lang == "en" else
            f"<b>{label}</b> 카테고리를 선택하세요:"
        )
        await query.edit_message_text(
            prompt,
            reply_markup=kb_category(lang, company),
            parse_mode=ParseMode.HTML,
        )

    # ── Company selection ───────────────────────────────────────────────
    elif data.startswith("company:"):
        company = data.split(":")[1]
        log_event("click", user.id, user.username, chat_id, data)
        label  = _company_label(company, lang)
        prompt = (
            f"Select category for <b>{label}</b>:"
            if lang == "en" else
            f"<b>{label}</b> 카테고리를 선택하세요:"
        )
        await query.edit_message_text(
            prompt,
            reply_markup=kb_category(lang, company),
            parse_mode=ParseMode.HTML,
        )

    # ── Category selection ──────────────────────────────────────────────
    elif data.startswith("cat:"):
        parts    = data.split(":")
        category = parts[1]
        company  = parts[2]
        log_event("click", user.id, user.username, chat_id, data)

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
        text = _fetch_text(company, category, lang)
        await query.edit_message_text(
            text,
            reply_markup=kb_after_result(lang, company),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )

    else:
        log.warning("Unhandled callback: %s", data)


# ── Free text (search) ────────────────────────────────────────────────────────

async def message_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user    = update.effective_user
    chat_id = update.effective_chat.id
    lang    = get_lang(chat_id)
    text    = (update.message.text or "").strip()

    pending = ctx.user_data.get("pending_search")
    if pending:
        ctx.user_data.pop("pending_search")
        company = pending["company"]
        log_event("search", user.id, user.username, chat_id, f"{company}|{text[:200]}")
        await _do_search(update, lang, company, text)
        return

    company  = _detect_company(text)
    category = _detect_category(text, company)

    if company and category:
        log_event("search", user.id, user.username, chat_id, f"{company}|{category}|{text[:200]}")
        result = _fetch_text(company, category, lang)
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


async def _do_search(update, lang, company, query_text):
    category = _detect_category(query_text, company) or "news"
    result   = _fetch_text(company, category, lang)
    await update.message.reply_text(
        result,
        reply_markup=kb_after_result(lang, company),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


# ── Shared fetch helper ───────────────────────────────────────────────────────

def _fetch_text(company: str, category: str, lang: str) -> str:
    """Fetch and format results. MicroStrategy never calls DART."""
    if category == "disclosures" and company in _DART_COMPANIES:
        return fmt_disclosures(get_disclosures(company), lang)
    else:
        return fmt_news(get_news(company), lang)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _company_label(key: str, lang: str) -> str:
    mapping = {
        "bitmax":        {"en": "Bitmax",    "ko": "비트맥스"},
        "bitplanet":     {"en": "Bitplanet", "ko": "비트플래닛"},
        "microstrategy": {"en": "Strategy",  "ko": "스트래티지"},
    }
    return mapping.get(key, {}).get(lang, key)


def _detect_company(text: str) -> str:
    t = text.lower()
    if "비트맥스" in t or "bitmax" in t:
        return "bitmax"
    if "비트플래닛" in t or "bitplanet" in t:
        return "bitplanet"
    if "microstrategy" in t or "mstr" in t or "스트래티지" in t or "strategy" in t:
        return "microstrategy"
    return ""


def _detect_category(text: str, company: str = "") -> str:
    """
    For MicroStrategy, always return 'news' (no DART).
    For others, detect from keywords.
    """
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