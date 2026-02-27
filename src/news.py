"""
news.py — RSS (Google News) + GDELT fallback
"""

import logging
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

import feedparser
import httpx

log = logging.getLogger(__name__)
SEOUL = ZoneInfo("Asia/Seoul")


COMPANY_QUERIES = {
    "bitmax":        ["비트맥스", "Bitmax Korea"],
    "bitplanet":     ["비트플래닛", "Bitplanet Korea"],
    "microstrategy": ["MicroStrategy", "MSTR bitcoin", "Strategy MicroStrategy"],
}


def _rss_url(query: str) -> str:
    q = urllib.parse.quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"


def _parse_time(entry) -> str:
    """Return time string from feed entry."""
    try:
        t = entry.get("published_parsed") or entry.get("updated_parsed")
        if t:
            dt = datetime(*t[:6])
            return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    return ""


def _fetch_rss(query: str, limit: int = 5) -> list[dict]:
    url = _rss_url(query)
    try:
        feed = feedparser.parse(url)
        items = []
        for entry in feed.entries[:limit]:
            items.append({
                "title":     entry.get("title", ""),
                "publisher": entry.get("source", {}).get("title", ""),
                "time":      _parse_time(entry),
                "url":       entry.get("link", ""),
            })
        return items
    except Exception as e:
        log.warning("RSS fetch failed for '%s': %s", query, e)
        return []


def _fetch_gdelt(company_key: str, limit: int = 5) -> list[dict]:
    """GDELT fallback — article search API."""
    queries = COMPANY_QUERIES.get(company_key, [company_key])
    query   = urllib.parse.quote(queries[0])
    url     = (
        "https://api.gdeltproject.org/api/v2/doc/doc"
        f"?query={query}&mode=artlist&maxrecords={limit}&format=json"
    )
    try:
        with httpx.Client(timeout=15) as client:
            r = client.get(url)
        r.raise_for_status()
        articles = r.json().get("articles", [])
        return [
            {
                "title":     a.get("title", ""),
                "publisher": a.get("domain", ""),
                "time":      a.get("seendate", "")[:16].replace("T", " ") if a.get("seendate") else "",
                "url":       a.get("url", ""),
            }
            for a in articles[:limit]
        ]
    except Exception as e:
        log.warning("GDELT fetch failed: %s", e)
        return []


def get_news(company_key: str, limit: int = 5) -> list[dict]:
    queries   = COMPANY_QUERIES.get(company_key.lower(), [company_key])
    results   = []
    seen_urls = set()

    for q in queries:
        for item in _fetch_rss(q, limit):
            if item["url"] not in seen_urls:
                seen_urls.add(item["url"])
                results.append(item)
        if len(results) >= limit:
            break

    # Fallback: GDELT
    if len(results) < limit:
        for item in _fetch_gdelt(company_key, limit):
            if item["url"] not in seen_urls:
                seen_urls.add(item["url"])
                results.append(item)

    return results[:limit]