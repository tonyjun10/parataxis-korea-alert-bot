"""
keyboards.py — All inline keyboard layouts.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# Companies that support Disclosures (DART): all except microstrategy
_DART_COMPANIES = {"bitmax", "bitplanet"}


# ── Language selection (Step 1) ────────────────────────────────────────────────

def kb_language() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("English", callback_data="lang:en")],
        [InlineKeyboardButton("한국어",   callback_data="lang:ko")],
    ])


# ── Company selection (Step 2) — now includes MicroStrategy ───────────────────

def kb_company(lang: str) -> InlineKeyboardMarkup:
    if lang == "ko":
        rows = [
            [InlineKeyboardButton("비트맥스",   callback_data="company:bitmax")],
            [InlineKeyboardButton("비트플래닛", callback_data="company:bitplanet")],
            [InlineKeyboardButton("스트래티지", callback_data="company:microstrategy")],
            [InlineKeyboardButton("🏠 Home",    callback_data="nav:home")],
        ]
    else:
        rows = [
            [InlineKeyboardButton("Bitmax",      callback_data="company:bitmax")],
            [InlineKeyboardButton("Bitplanet",   callback_data="company:bitplanet")],
            [InlineKeyboardButton("Strategy",    callback_data="company:microstrategy")],
            [InlineKeyboardButton("🏠 Home",     callback_data="nav:home")],
        ]
    return InlineKeyboardMarkup(rows)


# ── Category selection (Step 3) — varies by company ──────────────────────────

def kb_category(lang: str, company: str) -> InlineKeyboardMarkup:
    """
    Bitmax / Bitplanet: Disclosures + News + Search
    MicroStrategy:      News + Search only (no DART)
    """
    nav = [
        InlineKeyboardButton("⬅️ Back", callback_data="nav:back_to_company"),
        InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
    ]

    if company in _DART_COMPANIES:
        if lang == "ko":
            rows = [
                [InlineKeyboardButton("공시", callback_data=f"cat:disclosures:{company}")],
                [InlineKeyboardButton("기사", callback_data=f"cat:news:{company}")],
                [InlineKeyboardButton("검색", callback_data=f"cat:search:{company}")],
                nav,
            ]
        else:
            rows = [
                [InlineKeyboardButton("Disclosures", callback_data=f"cat:disclosures:{company}")],
                [InlineKeyboardButton("News",        callback_data=f"cat:news:{company}")],
                [InlineKeyboardButton("Search",      callback_data=f"cat:search:{company}")],
                nav,
            ]
    else:
        # MicroStrategy — no Disclosures tab
        if lang == "ko":
            rows = [
                [InlineKeyboardButton("기사", callback_data=f"cat:news:{company}")],
                [InlineKeyboardButton("검색", callback_data=f"cat:search:{company}")],
                nav,
            ]
        else:
            rows = [
                [InlineKeyboardButton("News",   callback_data=f"cat:news:{company}")],
                [InlineKeyboardButton("Search", callback_data=f"cat:search:{company}")],
                nav,
            ]

    return InlineKeyboardMarkup(rows)


# ── After result display ───────────────────────────────────────────────────────

def kb_after_result(lang: str, company: str) -> InlineKeyboardMarkup:
    if lang == "ko":
        rows = [[
            InlineKeyboardButton("⬅️ 뒤로", callback_data=f"nav:back_to_cat:{company}"),
            InlineKeyboardButton("🏠 Home",  callback_data="nav:home"),
        ]]
    else:
        rows = [[
            InlineKeyboardButton("⬅️ Back", callback_data=f"nav:back_to_cat:{company}"),
            InlineKeyboardButton("🏠 Home",  callback_data="nav:home"),
        ]]
    return InlineKeyboardMarkup(rows)


# ── /watch category selection keyboard ────────────────────────────────────────

def kb_watch_categories(lang: str) -> InlineKeyboardMarkup:
    """Shown when user sends /watch with no argument."""
    if lang == "ko":
        rows = [
            [InlineKeyboardButton("📋 공시", callback_data="watch:disclosures")],
            [InlineKeyboardButton("📰 기사", callback_data="watch:news")],
            [InlineKeyboardButton("✅ 전체", callback_data="watch:all")],
        ]
    else:
        rows = [
            [InlineKeyboardButton("📋 Disclosures", callback_data="watch:disclosures")],
            [InlineKeyboardButton("📰 News",        callback_data="watch:news")],
            [InlineKeyboardButton("✅ All",          callback_data="watch:all")],
        ]
    return InlineKeyboardMarkup(rows)


def kb_unwatch_categories(lang: str) -> InlineKeyboardMarkup:
    """Shown when user sends /unwatch with no argument."""
    if lang == "ko":
        rows = [
            [InlineKeyboardButton("📋 공시",      callback_data="unwatch:disclosures")],
            [InlineKeyboardButton("📰 기사",      callback_data="unwatch:news")],
            [InlineKeyboardButton("🔕 전체 해제", callback_data="unwatch:all")],
        ]
    else:
        rows = [
            [InlineKeyboardButton("📋 Disclosures",     callback_data="unwatch:disclosures")],
            [InlineKeyboardButton("📰 News",             callback_data="unwatch:news")],
            [InlineKeyboardButton("🔕 Unsubscribe All",  callback_data="unwatch:all")],
        ]
    return InlineKeyboardMarkup(rows)