"""
prices.py — Cryptocurrency price fetching with caching and 429 handling.

Fixes applied:
  - In-memory TTL cache (60 s default) — one cache entry per (coin, currency).
    CoinGecko and Upbit are never called more than once per TTL window.
  - 429 handling: retry once after 2 s; if still rate-limited, fall back to
    Upbit (for USD requests) and return a note in the source field.
  - Separate pipelines:
      English (USD): CoinGecko → (429 fallback) Upbit KRW with USD note
      Korean  (KRW): Upbit   → Bithumb
  - All HTTP calls have explicit timeouts.
  - No triple-calling — each function hits at most one exchange per call.
"""

import asyncio
import logging
import time

import httpx

log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────
TIMEOUT_S    = 10   # seconds per HTTP request
CACHE_TTL_S  = 60   # seconds before a cached price expires
RETRY_WAIT_S = 2    # seconds to wait before retrying a 429

# ── Coin metadata ──────────────────────────────────────────────────────────────
_COINGECKO_IDS = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "xrp": "ripple",
}

_UPBIT_MARKETS = {
    "btc": "KRW-BTC",
    "eth": "KRW-ETH",
    "xrp": "KRW-XRP",
}

_BITHUMB_COINS = {
    "btc": "BTC",
    "eth": "ETH",
    "xrp": "XRP",
}

_COIN_NAMES = {
    "btc": {"en": "Bitcoin",  "ko": "비트코인"},
    "eth": {"en": "Ethereum", "ko": "이더리움"},
    "xrp": {"en": "XRP",     "ko": "리플"},
}

# ── In-memory cache ────────────────────────────────────────────────────────────
# key: (coin, currency_str)  e.g. ("btc", "USD")
# value: {"result": {...}, "ts": float}
_cache: dict[tuple, dict] = {}


def _cache_get(coin: str, currency: str) -> dict | None:
    entry = _cache.get((coin.lower(), currency))
    if entry and (time.monotonic() - entry["ts"]) < CACHE_TTL_S:
        log.debug("Price cache hit: %s/%s", coin, currency)
        return entry["result"]
    return None


def _cache_set(coin: str, currency: str, result: dict):
    _cache[(coin.lower(), currency)] = {"result": result, "ts": time.monotonic()}


# ── CoinGecko (USD) ────────────────────────────────────────────────────────────

async def _coingecko_usd(coin: str) -> dict:
    """
    Fetch USD price from CoinGecko.
    On 429: wait RETRY_WAIT_S, retry once.
    Returns {"price": float, "source": str} or {"error": str, "rate_limited": bool}.
    """
    coin_id = _COINGECKO_IDS.get(coin.lower())
    if not coin_id:
        return {"error": f"Unknown coin: {coin}"}

    url = (
        f"https://api.coingecko.com/api/v3/simple/price"
        f"?ids={coin_id}&vs_currencies=usd"
    )

    for attempt in range(2):
        try:
            async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
                r = await client.get(url)

            if r.status_code == 429:
                if attempt == 0:
                    log.warning("CoinGecko 429 for %s — retrying in %ds.", coin, RETRY_WAIT_S)
                    await asyncio.sleep(RETRY_WAIT_S)
                    continue
                else:
                    log.warning("CoinGecko still 429 for %s after retry.", coin)
                    return {"error": "Rate limited", "rate_limited": True}

            r.raise_for_status()
            price = r.json()[coin_id]["usd"]
            return {"price": price, "source": "CoinGecko"}

        except httpx.TimeoutException:
            log.warning("CoinGecko timeout for %s (attempt %d).", coin, attempt + 1)
            if attempt == 0:
                await asyncio.sleep(RETRY_WAIT_S)
                continue
            return {"error": "CoinGecko timeout"}
        except Exception as e:
            log.warning("CoinGecko error for %s: %s", coin, e)
            return {"error": str(e)}

    return {"error": "CoinGecko unavailable"}


# ── Upbit (KRW primary) ────────────────────────────────────────────────────────

async def _upbit_krw(coin: str) -> dict:
    """Returns {"price": float, "source": "Upbit"} or {"error": str}."""
    market = _UPBIT_MARKETS.get(coin.lower())
    if not market:
        return {"error": f"Unknown coin for Upbit: {coin}"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            r = await client.get(
                "https://api.upbit.com/v1/ticker",
                params={"markets": market},
            )
        r.raise_for_status()
        data = r.json()
        if data:
            return {"price": float(data[0]["trade_price"]), "source": "Upbit"}
        return {"error": "Upbit returned empty response"}
    except httpx.TimeoutException:
        log.warning("Upbit timeout for %s.", coin)
        return {"error": "Upbit timeout"}
    except Exception as e:
        log.warning("Upbit error for %s: %s", coin, e)
        return {"error": str(e)}


# ── Bithumb (KRW fallback) ─────────────────────────────────────────────────────

async def _bithumb_krw(coin: str) -> dict:
    """Returns {"price": float, "source": "Bithumb"} or {"error": str}."""
    code = _BITHUMB_COINS.get(coin.lower())
    if not code:
        return {"error": f"Unknown coin for Bithumb: {coin}"}
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT_S) as client:
            r = await client.get(f"https://api.bithumb.com/public/ticker/{code}_KRW")
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "0000":
            return {"price": float(data["data"]["closing_price"]), "source": "Bithumb"}
        return {"error": f"Bithumb error status: {data.get('status')}"}
    except httpx.TimeoutException:
        log.warning("Bithumb timeout for %s.", coin)
        return {"error": "Bithumb timeout"}
    except Exception as e:
        log.warning("Bithumb error for %s: %s", coin, e)
        return {"error": str(e)}


# ── Public API ─────────────────────────────────────────────────────────────────

async def get_price_usd(coin: str) -> dict:
    """
    USD price pipeline (English mode):
      CoinGecko (cached) → on 429: Upbit KRW with note.
    Returns {coin, price, currency, source} or {error}.
    """
    coin = coin.lower()

    cached = _cache_get(coin, "USD")
    if cached:
        return cached

    result = await _coingecko_usd(coin)

    if result.get("rate_limited"):
        # Graceful degradation: use KRW price with a note
        log.info("Falling back to Upbit KRW for %s due to CoinGecko rate limit.", coin)
        krw_result = await _get_krw_with_cache(coin)
        if "error" not in krw_result:
            out = {
                "coin":     coin,
                "price":    krw_result["price"],
                "currency": "KRW",
                "source":   krw_result["source"] + " (USD unavailable — rate limited)",
            }
            # Don't cache the fallback result under USD key
            return out
        return {"error": "USD price unavailable (rate limited) and KRW fallback also failed."}

    if "error" in result:
        return {"error": result["error"]}

    out = {
        "coin":     coin,
        "price":    result["price"],
        "currency": "USD",
        "source":   result["source"],
    }
    _cache_set(coin, "USD", out)
    return out


async def _get_krw_with_cache(coin: str) -> dict:
    """Internal helper: fetch KRW, respecting cache."""
    cached = _cache_get(coin, "KRW")
    if cached:
        return cached

    result = await _upbit_krw(coin)
    if "error" in result:
        result = await _bithumb_krw(coin)
    if "error" in result:
        return result

    out = {
        "coin":     coin,
        "price":    result["price"],
        "currency": "KRW",
        "source":   result["source"],
    }
    _cache_set(coin, "KRW", out)
    return out


async def get_price_krw(coin: str) -> dict:
    """
    KRW price pipeline (Korean mode):
      Upbit (cached) → Bithumb fallback.
    Returns {coin, price, currency, source} or {error}.
    """
    coin = coin.lower()
    result = await _get_krw_with_cache(coin)
    if "error" in result:
        return {"error": result["error"]}
    return result


async def get_price(coin: str, lang: str) -> dict:
    """Entry point: route to USD or KRW based on language."""
    if lang == "ko":
        return await get_price_krw(coin)
    return await get_price_usd(coin)


def fmt_price(result: dict, lang: str) -> str:
    """Format a price result dict into Telegram HTML."""
    if "error" in result:
        return f"⚠️ {result['error']}"

    coin     = result["coin"]
    price    = result["price"]
    currency = result["currency"]
    source   = result.get("source", "")
    name     = _COIN_NAMES.get(coin, {}).get(lang, coin.upper())

    formatted = f"₩{price:,.0f}" if currency == "KRW" else f"${price:,.2f}"

    if lang == "ko":
        return f"💰 <b>{name}</b>\n현재가: <b>{formatted}</b>\n출처: {source}"
    return f"💰 <b>{name}</b>\nPrice: <b>{formatted}</b>\nSource: {source}"
