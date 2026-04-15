"""
news.py — RSS (Google News) + GDELT fallback.
All blocking I/O is wrapped in asyncio.to_thread() so the Telegram
event loop is never blocked.
"""

import asyncio
import hashlib
import logging
import urllib.parse
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import feedparser
import httpx

log = logging.getLogger(__name__)

TIMEOUT = 15  # seconds per request

PARATAXIS_MAX_AGE_DAYS = 30

COMPANY_QUERIES: dict[str, list[str]] = {
    "parataxis": [
        "파라택시스코리아",
        "파라택시스 코리아",
        "288330",
        "KOSDAQ 288330",
        "PARATAXIS KOREA",
    ],
    "bitmax":        ["비트맥스", "Bitmax Korea"],
    "bitplanet":     ["비트플래닛", "Bitplanet Korea"],
    "parataxiseth":  ["파라택시스이더리움", "파라택시스 이더리움", "290560 KOSDAQ", "PARATAXIS ETHEREUM"],
    "microstrategy": ["MicroStrategy", "MSTR bitcoin", "Strategy MicroStrategy"],
    "parataxiseth":   ["파라택시스이더리움", "Parataxis Ethereum", "290560", "KOSDAQ 290560"],
}


# ── URL normalisation ──────────────────────────────────────────────────────────

def _normalise_url(url: str) -> str:
    """Strip tracking params and lowercase scheme+host for stable de-dup keys."""
    url = url.strip()
    try:
        p    = urllib.parse.urlparse(url)
        kept = [
            (k, v) for k, v in urllib.parse.parse_qsl(p.query)
            if not k.lower().startswith(("utm_", "ref", "source", "sid", "sref"))
        ]
        return urllib.parse.urlunparse(p._replace(
            scheme=p.scheme.lower(),
            netloc=p.netloc.lower(),
            query=urllib.parse.urlencode(kept),
        ))
    except Exception:
        return url


def _url_key(url: str) -> str:
    return hashlib.md5(_normalise_url(url).encode()).hexdigest()


# ── Time parsing ──────────────────────────────────────────────────────────────

def _parse_entry_dt(entry) -> datetime | None:
    """
    Return a timezone-aware datetime from a feedparser entry, or None.

    Tries the raw RFC-2822 string first (better tz fidelity), then falls back
    to feedparser's pre-parsed struct_time.

    Defensively normalises any naive datetime to UTC so comparisons with
    timezone-aware cutoff values never raise TypeError.
    """
    # Raw string path — preserves explicit timezone offset
    for field in ("published", "updated"):
        raw = entry.get(field, "")
        if raw:
            try:
                dt = parsedate_to_datetime(raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except Exception:
                pass

    # Struct-time fallback — treat as UTC
    for field in ("published_parsed", "updated_parsed"):
        t = entry.get(field)
        if t:
            try:
                return datetime(*t[:6], tzinfo=timezone.utc)
            except Exception:
                pass

    return None


def _dt_to_display(dt: datetime | None) -> str:
    if dt is None:
        return ""
    try:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


# ── RSS fetch ──────────────────────────────────────────────────────────────────

def _rss_url(query: str) -> str:
    return (
        f"https://news.google.com/rss/search"
        f"?q={urllib.parse.quote(query)}&hl=ko&gl=KR&ceid=KR:ko"
    )


def _fetch_rss_sync(query: str, limit: int) -> list[dict]:
    """
    Fetch one RSS query. Items carry an internal '_dt' key (datetime | None)
    for sorting; strip it before returning results to consumers.
    """
    try:
        feed = feedparser.parse(_rss_url(query))
        items = []
        for e in feed.entries[:limit]:
            dt = _parse_entry_dt(e)
            items.append({
                "title":     e.get("title", ""),
                "publisher": e.get("source", {}).get("title", ""),
                "time":      _dt_to_display(dt),
                "url":       e.get("link", ""),
                "_dt":       dt,
            })
        return items
    except Exception as exc:
        log.warning("RSS fetch failed for '%s': %s", query, exc)
        return []


def _strip_dt(item: dict) -> dict:
    return {k: v for k, v in item.items() if k != "_dt"}


# ── Parataxis Korea pipeline ───────────────────────────────────────────────────

def _get_parataxis_news_sync(limit: int) -> list[dict]:
    """
    Run all Parataxis query variants, merge results, de-dup by normalised URL,
    discard items older than PARATAXIS_MAX_AGE_DAYS, sort newest-first.
    """
    cutoff     = datetime.now(timezone.utc) - timedelta(days=PARATAXIS_MAX_AGE_DAYS)
    seen_keys: set[str] = set()
    merged:    list[dict] = []

    for query in COMPANY_QUERIES["parataxis"]:
        for item in _fetch_rss_sync(query, limit):
            url = item.get("url", "")
            if not url:
                continue
            key = _url_key(url)
            if key in seen_keys:
                continue
            seen_keys.add(key)

            dt: datetime | None = item["_dt"]
            if dt is not None and dt < cutoff:
                log.debug("Parataxis: dropping old item (%s) %s", item["time"], item["title"][:60])
                continue

            merged.append(item)

    merged.sort(
        key=lambda it: it["_dt"] if it["_dt"] is not None else datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    result = [_strip_dt(it) for it in merged[:limit]]
    log.info("Parataxis news: %d items after merge/filter/sort (limit=%d)", len(result), limit)
    return result


# ── GDELT fallback ─────────────────────────────────────────────────────────────

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
        return [
            {
                "title":     a.get("title", ""),
                "publisher": a.get("domain", ""),
                "time":      a.get("seendate", "")[:16].replace("T", " ") if a.get("seendate") else "",
                "url":       a.get("url", ""),
            }
            for a in r.json().get("articles", [])[:limit]
        ]
    except Exception as exc:
        log.warning("GDELT fetch failed for %s: %s", company_key, exc)
        return []


# ── Generic pipeline (bitmax / bitplanet / microstrategy) ─────────────────────

def _get_news_sync(company_key: str, limit: int = 5) -> list[dict]:
    queries   = COMPANY_QUERIES.get(company_key.lower(), [company_key])
    results   = []
    seen_urls: set[str] = set()

    for q in queries:
        for item in _fetch_rss_sync(q, limit):
            url = item.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                results.append(_strip_dt(item))
        if len(results) >= limit:
            break

    if len(results) < limit:
        for item in _fetch_gdelt_sync(company_key, limit):
            url = item.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                results.append(item)

    return results[:limit]


# ── Public async entry point ──────────────────────────────────────────────────

async def get_news(company_key: str, limit: int = 5) -> list[dict]:
    """Async entry point — runs blocking fetch in a thread pool."""
    key = company_key.lower()
    if key == "parataxis":
        return await asyncio.to_thread(_get_parataxis_news_sync, limit)
    return await asyncio.to_thread(_get_news_sync, key, limit)
