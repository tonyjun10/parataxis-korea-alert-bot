"""
formatter.py — Format disclosures, news, and strategy into Telegram messages.
"""

from html import escape


def fmt_disclosures(items: list[dict], lang: str) -> str:
    if not items:
        return "📭 No disclosures found." if lang == "en" else "📭 공시 없음."
    if "error" in items[0]:
        return f"⚠️ {items[0]['error']}"
    label = "Disclosures" if lang == "en" else "공시"
    lines = [f"<b>📋 {label} (최신 {len(items)}건)</b>\n"]
    for i, it in enumerate(items, 1):
        lines.append(
            f"{i}. <b>{escape(it['title'])}</b>\n"
            f"   📅 {it['date']} | #{it['rcept_no']}\n"
            f"   🔗 <a href=\"{it['url']}\">DART 보기</a>"
        )
    return "\n".join(lines)


def fmt_news(items: list[dict], lang: str) -> str:
    if not items:
        return "📭 No news found." if lang == "en" else "📭 기사 없음."
    label = "News" if lang == "en" else "기사"
    lines = [f"<b>📰 {label} (최신 {len(items)}건)</b>\n"]
    for i, it in enumerate(items, 1):
        pub = f" — {escape(it['publisher'])}" if it.get("publisher") else ""
        t   = f" | {it['time']}" if it.get("time") else ""
        lines.append(
            f"{i}. <a href=\"{it['url']}\">{escape(it['title'])}</a>"
            f"\n   {pub}{t}"
        )
    return "\n".join(lines)


def fmt_strategy(items: list[dict], lang: str) -> str:
    if not items:
        return (
            "📭 No strategy-relevant items found."
            if lang == "en" else
            "📭 전략 관련 항목 없음."
        )
    label = "Strategy" if lang == "en" else "전략"
    lines = [f"<b>♟ {label} (최신 {len(items)}건)</b>\n"]
    for i, it in enumerate(items, 1):
        source = it.get("source", "")
        if source == "disclosure":
            badge = "📋"
            detail = (
                f"   📅 {it.get('date','')} | #{it.get('rcept_no','')}\n"
                f"   🔗 <a href=\"{it.get('url','')}\">DART 보기</a>"
            )
        else:
            badge = "📰"
            pub    = f" — {escape(it['publisher'])}" if it.get("publisher") else ""
            t      = f" | {it.get('time','')}" if it.get("time") else ""
            detail = f"   {badge}{pub}{t}\n   🔗 <a href=\"{it.get('url','')}\">링크</a>"

        lines.append(f"{i}. {badge} <b>{escape(it.get('title',''))}</b>\n{detail}")
    return "\n".join(lines)