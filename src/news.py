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
    "parataxiseth":  ["파라택시스이더리움", "파라택시스 이더리움", "Parataxis Ethereum", "신시웨이", "Sinsiway", "290560 KOSDAQ"],
    "microstrategy": ["MicroStrategy", "MSTR bitcoin", "Strategy MicroStrategy"],
    "bitmine":       ["Bitmine Immersion", "BMNR", "Bitmine Ethereum", "비트마인"],
    "market_news":   [
        # Korean — regulation / policy / legislation
        "가상자산 규제", "디지털자산법", "디지털자산기본법",
        "가상자산 이용자보호법", "스테이블코인 규제", "가상자산 과세",
        "코인 입법", "가상자산 입법", "CBDC", "클래리티법",
        "FIU 가상자산", "가상자산사업자",
        # Korean — institutional / treasury / macro
        "비트코인 기관투자", "비트코인 보유 기업", "가상자산 보유 기업",
        "비트코인 재무", "메타플래닛", "한국은행 가상자산",
        "비트코인 보안", "양자컴 비트코인",
        # Korean — companies / ecosystem
        "스트래티지 비트코인", "마이크로스트래티지",
        "이더리움 재단", "이더리움 생태계", "비트마인",
        "솔라나", "리플 XRP", "XRP 규제",
        # Korean — ETF / products
        "비트코인 ETF", "이더리움 ETF", "가상자산 ETF",
        # English — institutional / regulatory
        "Bitcoin treasury company", "Bitcoin ETF institutional",
        "crypto regulation", "stablecoin bill", "digital asset law",
        "MicroStrategy Bitcoin", "Metaplanet Bitcoin",
        "Ethereum ETF", "Ethereum Foundation", "SEC crypto",
        "corporate Bitcoin holdings", "XRP ETF", "Solana institutional",
    ],
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
        feed = feedparser.parse(
            _rss_url(query),
            agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
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

def _get_parataxis_news_sync(limit: int, no_age_limit: bool = False) -> list[dict]:
    """
    Run all Parataxis query variants, merge results, de-dup by normalised URL,
    discard items older than PARATAXIS_MAX_AGE_DAYS (unless no_age_limit=True), sort newest-first.
    """
    cutoff     = None if no_age_limit else datetime.now(timezone.utc) - timedelta(days=PARATAXIS_MAX_AGE_DAYS)
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
            if cutoff is not None and dt is not None and dt < cutoff:
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


# ── Generic pipeline (bitmax / bitplanet / microstrategy / market_news) ────────

def _get_news_sync(company_key: str, limit: int = 5, max_age_days: int | None = None) -> list[dict]:
    key       = company_key.lower()
    queries   = COMPANY_QUERIES.get(key, [company_key])
    results   = []
    seen_urls: set[str] = set()

    cutoff = None
    if max_age_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=max_age_days)

    # For market_news we want VOLUME: query every keyword and don't stop early.
    # But we throttle requests to avoid Google News rate-limiting (serving empty
    # feeds), which happens when we hammer it too fast. 12 per query is plenty.
    is_market = (key == "market_news")
    per_query_fetch = 20 if is_market else limit

    import time as _time
    for i, q in enumerate(queries):
        for item in _fetch_rss_sync(q, per_query_fetch):
            url = item.get("url", "")
            if not url or url in seen_urls:
                continue
            # Age filter: drop items older than cutoff.
            # Items with an unparseable date (_dt is None) are dropped when a
            # cutoff is set, since we can't verify they're recent.
            if cutoff is not None:
                dt = item.get("_dt")
                if dt is None or dt < cutoff:
                    continue
            seen_urls.add(url)
            results.append(item)  # keep _dt for now; we sort then strip below
        if not is_market and len(results) >= limit:
            break
        # Throttle: small pause between queries so we don't trip Google News
        # rate-limiting. Only needed for market_news (many keywords per cycle).
        if is_market and i < len(queries) - 1:
            _time.sleep(0.4)

    # GDELT fallback only when we still need more AND no strict age filter
    if len(results) < limit and cutoff is None and not is_market:
        for item in _fetch_gdelt_sync(company_key, limit):
            url = item.get("url", "")
            if url and url not in seen_urls:
                seen_urls.add(url)
                results.append(item)

    # Sort newest-first so the freshest articles are kept, then strip _dt
    def _key(it):
        dt = it.get("_dt")
        return dt if dt is not None else datetime.min.replace(tzinfo=timezone.utc)
    results.sort(key=_key, reverse=True)
    results = [_strip_dt(it) if "_dt" in it else it for it in results]

    # market_news: return the whole fresh set (capped generously) so the
    # scheduler can log them all. Other feeds: keep the tight limit.
    if is_market:
        return results[:50]
    return results[:limit]



def _get_parataxiseth_news_sync(limit: int, no_age_limit: bool = False) -> list[dict]:
    """
    Run all Parataxis Ethereum / Sinsiway query variants, merge, de-dup, sort newest-first.
    Searches both new branding (Parataxis Ethereum) and old (Sinsiway/신시웨이).
    """
    cutoff     = None if no_age_limit else datetime.now(timezone.utc) - timedelta(days=PARATAXIS_MAX_AGE_DAYS)
    seen_keys: set[str] = set()
    merged:    list[dict] = []

    for query in COMPANY_QUERIES["parataxiseth"]:
        for item in _fetch_rss_sync(query, limit):
            url = item.get("url", "")
            if not url:
                continue
            key = _url_key(url)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            dt: datetime | None = item["_dt"]
            if cutoff is not None and dt is not None and dt < cutoff:
                continue
            merged.append(item)

    merged.sort(
        key=lambda it: it["_dt"] if it["_dt"] is not None else datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    result = [_strip_dt(it) for it in merged[:limit]]
    log.info("Parataxis ETH news: %d items after merge/filter/sort (limit=%d)", len(result), limit)
    return result


# ── Public async entry point ──────────────────────────────────────────────────

async def get_news(company_key: str, limit: int = 5, no_age_limit: bool = False) -> list[dict]:
    """Async entry point — runs blocking fetch in a thread pool.
    Pass no_age_limit=True for on-demand fetches to bypass the age cutoff.
    """
    key = company_key.lower()
    if key == "parataxis":
        return await asyncio.to_thread(_get_parataxis_news_sync, limit, no_age_limit)
    if key == "parataxiseth":
        return await asyncio.to_thread(_get_parataxiseth_news_sync, limit, no_age_limit)
    # market_news gets a strict 3-day age filter so only genuinely recent
    # articles surface (Google News RSS ranks by relevance, not date, so
    # without this it serves stale-but-relevant articles).
    max_age = None if no_age_limit else (3 if key == "market_news" else None)
    return await asyncio.to_thread(_get_news_sync, key, limit, max_age)
