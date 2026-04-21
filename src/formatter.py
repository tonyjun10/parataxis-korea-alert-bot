"""
formatter.py — Format data into Telegram-ready HTML strings.
"""

from html import escape


DISPLAY_LIMIT = 3  # max items shown per section in on-demand views


def fmt_disclosures(items: list[dict], lang: str, limit: int = DISPLAY_LIMIT) -> str:
    if not items:
        return "📭 No disclosures found." if lang == "en" else "📭 공시 없음."
    if "error" in items[0]:
        return f"⚠️ {items[0]['error']}"
    shown = items[:limit]
    label = "Disclosures" if lang == "en" else "공시"
    lines = [f"<b>📋 {label} (최신 {len(shown)}건)</b>\n"]
    for i, it in enumerate(shown, 1):
        lines.append(
            f"{i}. <b>{escape(it['title'])}</b>\n"
            f"   📅 {it['date']} | #{it['rcept_no']}\n"
            f"   🔗 <a href=\"{it['url']}\">DART 보기</a>"
        )
    return "\n".join(lines)


def fmt_news(items: list[dict], lang: str, limit: int = DISPLAY_LIMIT) -> str:
    if not items:
        return "📭 No news found." if lang == "en" else "📭 기사 없음."
    shown = items[:limit]
    label = "News" if lang == "en" else "기사"
    lines = [f"<b>📰 {label} (최신 {len(shown)}건)</b>\n"]
    for i, it in enumerate(shown, 1):
        pub = f" — {escape(it['publisher'])}" if it.get("publisher") else ""
        t   = f" | {it['time']}" if it.get("time") else ""
        lines.append(
            f"{i}. <a href=\"{it['url']}\">{escape(it['title'])}</a>"
            f"\n   {pub}{t}"
        )
    return "\n".join(lines)
