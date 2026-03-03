"""
news.py — RSS (Google News) + GDELT fallback.
All blocking I/O is wrapped in asyncio.to_thread() so the Telegram
event loop is never blocked.
"""

import asyncio
import logging
import urllib.parse
from datetime import datetime

import feedparser
import httpx

log = logging.getLogger(__name__)

TIMEOUT = 15  # seconds per request

COMPANY_QUERIES: dict[str, list[str]] = {
    "parataxis":     ["파라택시스", "Parataxis Korea"],
    "bitmax":        ["비트맥스", "Bitmax Korea"],
    "bitplanet":     ["비트플래닛", "Bitplanet Korea"],
    "microstrategy": ["MicroStrategy", "MSTR bitcoin", "Strategy MicroStrategy"],
}


def _rss_url(query: str) -> str:
    q = urllib.parse.quote(query)
    return f"https://news.google.com/rss/search?q={q}&hl=ko&gl=KR&ceid=KR:ko"


def _parse_time(entry) -> str:
    try:
        t = entry.get("published_parsed") or entry.get("updated_parsed")
        if t:
            return datetime(*t[:6]).strftime("%Y-%m-%d %H:%M")
    except Exception:
        pass
    return ""


def _fetch_rss_sync(query: str, limit: int) -> list[dict]:
    try:
        feed = feedparser.parse(_rss_url(query))
        return [
            {
                "title":     e.get("title", ""),
                "publisher": e.get("source", {}).get("title", ""),
                "time":      _parse_time(e),
                "url":       e.get("link", ""),
            }
            for e in feed.entries[:limit]
        ]
    except Exception as e:
        log.warning("RSS fetch failed for '%s': %s", query, e)
        return []


def _fetch_gdelt_sync(company_key: str, limit: int) -> list[dict]:
    queries = COMPANY_QUERIES.get(company_key, [company_key])
    query   = urllib.parse.quote(queries[0])
    url     = (
        "https://api.gdeltproject.org/api/v2/doc/doc"
        f"?query={query}&mode=artlist&maxrecords={limit}&format=json"
    )
    try:
        with httpx.Client(timeout=TIMEOUT) as client:
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
        log.warning("GDELT fetch failed for %s: %s", company_key, e)
        return []


def _get_news_sync(company_key: str, limit: int = 5) -> list[dict]:
    """Synchronous version — called via asyncio.to_thread."""
    queries   = COMPANY_QUERIES.get(company_key.lower(), [company_key])
    results   = []
    seen_urls: set[str] = set()

    for q in queries:
        for item in _fetch_rss_sync(q, limit):
            if item["url"] and item["url"] not in seen_urls:
                seen_urls.add(item["url"])
                results.append(item)
        if len(results) >= limit:
            break

    if len(results) < limit:
        for item in _fetch_gdelt_sync(company_key, limit):
            if item["url"] and item["url"] not in seen_urls:
                seen_urls.add(item["url"])
                results.append(item)

    return results[:limit]


async def get_news(company_key: str, limit: int = 5) -> list[dict]:
    """Async entry point — runs blocking fetch in a thread pool."""
    return await asyncio.to_thread(_get_news_sync, company_key, limit)
