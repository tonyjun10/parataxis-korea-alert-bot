"""
keyboards.py — All inline keyboard layouts.

Change: kb_price() now includes a Parataxis Korea (KOSDAQ 288330) button.
All other functions are completely unchanged.
"""

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# Companies that support DART disclosures
_DART_COMPANIES = {"parataxis", "bitmax", "bitplanet"}


# ── Step 1: Language ───────────────────────────────────────────────────────────

def kb_language() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("English", callback_data="lang:en")],
        [InlineKeyboardButton("한국어",   callback_data="lang:ko")],
    ])


# ── Step 2: Top-level menu ─────────────────────────────────────────────────────

def kb_main(lang: str) -> InlineKeyboardMarkup:
    """Top-level menu after language selection."""
    if lang == "ko":
        rows = [
            [InlineKeyboardButton("📁 파라택시스 코리아", callback_data="company:parataxis")],
            [InlineKeyboardButton("📁 비트맥스",         callback_data="company:bitmax")],
            [InlineKeyboardButton("📁 비트플래닛",       callback_data="company:bitplanet")],
            [InlineKeyboardButton("📁 스트래티지",       callback_data="company:microstrategy")],
            [InlineKeyboardButton("💰 가격",             callback_data="menu:price")],
            [InlineKeyboardButton("🔔 구독 관리",        callback_data="menu:subscribe")],
            [InlineKeyboardButton("📋 로그",             callback_data="menu:logs")],
        ]
    else:
        rows = [
            [InlineKeyboardButton("📁 Parataxis Korea", callback_data="company:parataxis")],
            [InlineKeyboardButton("📁 Bitmax",          callback_data="company:bitmax")],
            [InlineKeyboardButton("📁 Bitplanet",       callback_data="company:bitplanet")],
            [InlineKeyboardButton("📁 Strategy",        callback_data="company:microstrategy")],
            [InlineKeyboardButton("💰 Price",           callback_data="menu:price")],
            [InlineKeyboardButton("🔔 Subscribe",       callback_data="menu:subscribe")],
            [InlineKeyboardButton("📋 Logs",            callback_data="menu:logs")],
        ]
    return InlineKeyboardMarkup(rows)


# ── Step 3: Category per company ──────────────────────────────────────────────

def kb_category(lang: str, company: str) -> InlineKeyboardMarkup:
    """
    Parataxis / Bitmax / Bitplanet: Disclosures + News + Search
    MicroStrategy: News + Search only
    """
    nav = [
        InlineKeyboardButton("⬅️ Back", callback_data="nav:main"),
        InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
    ]
    if company in _DART_COMPANIES:
        if lang == "ko":
            rows = [
                [InlineKeyboardButton("📋 공시", callback_data=f"cat:disclosures:{company}")],
                [InlineKeyboardButton("📰 기사", callback_data=f"cat:news:{company}")],
                [InlineKeyboardButton("🔍 검색", callback_data=f"cat:search:{company}")],
                nav,
            ]
        else:
            rows = [
                [InlineKeyboardButton("📋 Disclosures", callback_data=f"cat:disclosures:{company}")],
                [InlineKeyboardButton("📰 News",        callback_data=f"cat:news:{company}")],
                [InlineKeyboardButton("🔍 Search",      callback_data=f"cat:search:{company}")],
                nav,
            ]
    else:
        if lang == "ko":
            rows = [
                [InlineKeyboardButton("📰 기사", callback_data=f"cat:news:{company}")],
                [InlineKeyboardButton("🔍 검색", callback_data=f"cat:search:{company}")],
                nav,
            ]
        else:
            rows = [
                [InlineKeyboardButton("📰 News",   callback_data=f"cat:news:{company}")],
                [InlineKeyboardButton("🔍 Search", callback_data=f"cat:search:{company}")],
                nav,
            ]
    return InlineKeyboardMarkup(rows)


# ── Price coin/stock selection ─────────────────────────────────────────────────
# CHANGED: added Parataxis Korea (KOSDAQ 288330) row

def kb_price(lang: str) -> InlineKeyboardMarkup:
    nav = [
        InlineKeyboardButton("⬅️ Back", callback_data="nav:main"),
        InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
    ]
    if lang == "ko":
        rows = [
            [InlineKeyboardButton("₿ 비트코인 (BTC)",           callback_data="price:btc")],
            [InlineKeyboardButton("Ξ 이더리움 (ETH)",           callback_data="price:eth")],
            [InlineKeyboardButton("✕ 리플 (XRP)",               callback_data="price:xrp")],
            [InlineKeyboardButton("📈 파라택시스 코리아 (288330)", callback_data="price:stock:288330")],
            nav,
        ]
    else:
        rows = [
            [InlineKeyboardButton("₿ Bitcoin (BTC)",                  callback_data="price:btc")],
            [InlineKeyboardButton("Ξ Ethereum (ETH)",                 callback_data="price:eth")],
            [InlineKeyboardButton("✕ XRP",                            callback_data="price:xrp")],
            [InlineKeyboardButton("📈 Parataxis Korea (KOSDAQ 288330)", callback_data="price:stock:288330")],
            nav,
        ]
    return InlineKeyboardMarkup(rows)


# ── After result ───────────────────────────────────────────────────────────────

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


def kb_after_price(lang: str) -> InlineKeyboardMarkup:
    if lang == "ko":
        rows = [[
            InlineKeyboardButton("⬅️ 뒤로", callback_data="menu:price"),
            InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
        ]]
    else:
        rows = [[
            InlineKeyboardButton("⬅️ Back", callback_data="menu:price"),
            InlineKeyboardButton("🏠 Home", callback_data="nav:home"),
        ]]
    return InlineKeyboardMarkup(rows)


# ── Subscribe menu keyboard ───────────────────────────────────────────────────

def kb_subscribe(lang: str) -> InlineKeyboardMarkup:
    """Subscription management menu accessible from the main menu."""
    nav = [
        InlineKeyboardButton("⬅️ 뒤로" if lang == "ko" else "⬅️ Back", callback_data="nav:main"),
        InlineKeyboardButton("🏠 Home",  callback_data="nav:home"),
    ]
    if lang == "ko":
        rows = [
            [InlineKeyboardButton("✅ 전체 구독",               callback_data="sub:all")],
            [InlineKeyboardButton("📁 파라택시스 코리아",        callback_data="sub:parataxis")],
            [InlineKeyboardButton("📁 비트맥스",                callback_data="sub:bitmax")],
            [InlineKeyboardButton("📁 비트플래닛",              callback_data="sub:bitplanet")],
            [InlineKeyboardButton("📁 스트래티지",              callback_data="sub:microstrategy")],
            [InlineKeyboardButton("💰 가격 업데이트",           callback_data="sub:brief")],
            [InlineKeyboardButton("⚡ 채굴 현황",               callback_data="sub:mining")],
            [InlineKeyboardButton("📅 데일리 스냅샷",           callback_data="sub:daily")],
            nav,
        ]
    else:
        rows = [
            [InlineKeyboardButton("✅ All",               callback_data="sub:all")],
            [InlineKeyboardButton("📁 Parataxis Korea",   callback_data="sub:parataxis")],
            [InlineKeyboardButton("📁 Bitmax",            callback_data="sub:bitmax")],
            [InlineKeyboardButton("📁 Bitplanet",         callback_data="sub:bitplanet")],
            [InlineKeyboardButton("📁 Strategy",          callback_data="sub:microstrategy")],
            [InlineKeyboardButton("💰 Price Updates",     callback_data="sub:brief")],
            [InlineKeyboardButton("⚡ Mining",            callback_data="sub:mining")],
            [InlineKeyboardButton("📅 Daily Snapshot",    callback_data="sub:daily")],
            nav,
        ]
    return InlineKeyboardMarkup(rows)


# ── /watch category keyboard ───────────────────────────────────────────────────

def kb_watch_categories(lang: str) -> InlineKeyboardMarkup:
    if lang == "ko":
        rows = [
            [InlineKeyboardButton("📋 공시 (전체)", callback_data="watch:all:disclosures")],
            [InlineKeyboardButton("📰 기사 (전체)", callback_data="watch:all:news")],
            [InlineKeyboardButton("✅ 전체 구독",   callback_data="watch:all:all")],
        ]
    else:
        rows = [
            [InlineKeyboardButton("📋 Disclosures (all companies)", callback_data="watch:all:disclosures")],
            [InlineKeyboardButton("📰 News (all companies)",        callback_data="watch:all:news")],
            [InlineKeyboardButton("✅ Subscribe to everything",      callback_data="watch:all:all")],
        ]
    return InlineKeyboardMarkup(rows)


def kb_unwatch_categories(lang: str) -> InlineKeyboardMarkup:
    if lang == "ko":
        rows = [
            [InlineKeyboardButton("📋 공시 해제",    callback_data="unwatch:all:disclosures")],
            [InlineKeyboardButton("📰 기사 해제",    callback_data="unwatch:all:news")],
            [InlineKeyboardButton("🔕 전체 해제",    callback_data="unwatch:all:all")],
        ]
    else:
        rows = [
            [InlineKeyboardButton("📋 Unsubscribe Disclosures", callback_data="unwatch:all:disclosures")],
            [InlineKeyboardButton("📰 Unsubscribe News",        callback_data="unwatch:all:news")],
            [InlineKeyboardButton("🔕 Unsubscribe everything",  callback_data="unwatch:all:all")],
        ]
    return InlineKeyboardMarkup(rows)


# ── Admin approval buttons ─────────────────────────────────────────────────────

def kb_approval(chat_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Approve", callback_data=f"approve:{chat_id}"),
        InlineKeyboardButton("❌ Deny",    callback_data=f"deny:{chat_id}"),
    ]])
