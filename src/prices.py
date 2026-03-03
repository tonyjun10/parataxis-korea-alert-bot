"""
prices.py — Cryptocurrency price fetching + Parataxis Korea (KOSDAQ 288330) stock price.

Crypto logic (BTC/ETH/XRP) is completely unchanged from the previous version.

New addition — Parataxis Korea stock:
  Source:  KRX (Korea Exchange) public JSON API — no API key, no extra packages.
           Uses the same httpx that is already a project dependency.
  Ticker:  288330  (KOSDAQ)
  Currency: KRW (both EN and KO modes)
  Cache:   TTL 45 s (STOCK_CACHE_TTL_S). On fetch failure the last cached value
           is returned with a "(cached)" label.
  Non-blocking: fetch runs via asyncio.to_thread() so the Telegram event loop
           is never blocked.
  Market closed: KRX returns the most recent close price even when the market
           is closed; we label it "Last close" in that case.
"""

import asyncio
import logging
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────
TIMEOUT_S        = 10   # seconds per HTTP request (crypto)
CACHE_TTL_S      = 60   # seconds — crypto price cache TTL
RETRY_WAIT_S     = 2    # seconds before retrying a CoinGecko 429
STOCK_CACHE_TTL_S = 45  # seconds — stock price cache TTL
STOCK_TIMEOUT_S   = 12  # seconds — KRX HTTP timeout

SEOUL = ZoneInfo("Asia/Seoul")

# ── Coin metadata (crypto) ─────────────────────────────────────────────────────
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

# ── In-memory cache (shared for crypto + stock) ────────────────────────────────
# key: (symbol, currency_str)  e.g. ("btc", "USD") or ("288330", "KRW")
# value: {"result": {...}, "ts": float}
_cache: dict[tuple, dict] = {}


def _cache_get(symbol: str, currency: str) -> dict | None:
    entry = _cache.get((symbol.lower(), currency))
    if entry and (time.monotonic() - entry["ts"]) < CACHE_TTL_S:
        log.debug("Price cache hit: %s/%s", symbol, currency)
        return entry["result"]
    return None


def _cache_get_stock(ticker: str) -> dict | None:
    entry = _cache.get((ticker, "KRW_STOCK"))
    if entry and (time.monotonic() - entry["ts"]) < STOCK_CACHE_TTL_S:
        log.debug("Stock cache hit: %s", ticker)
        return entry["result"]
    return None


def _cache_set(symbol: str, currency: str, result: dict):
    _cache[(symbol.lower(), currency)] = {"result": result, "ts": time.monotonic()}


def _cache_set_stock(ticker: str, result: dict):
    _cache[(ticker, "KRW_STOCK")] = {"result": result, "ts": time.monotonic()}


def _cache_get_stock_stale(ticker: str) -> dict | None:
    """Return cached stock result regardless of TTL — used as fallback on error."""
    entry = _cache.get((ticker, "KRW_STOCK"))
    return entry["result"] if entry else None


# ── Parataxis Korea stock price (KRX public API) ───────────────────────────────

PARATAXIS_TICKER = "288330"

# KRX open-market hours (KST): Mon–Fri 09:00–15:30
_MARKET_OPEN_H  = 9
_MARKET_OPEN_M  = 0
_MARKET_CLOSE_H = 15
_MARKET_CLOSE_M = 30


def _is_market_open() -> bool:
    """Rough check: is KOSDAQ currently open?"""
    now = datetime.now(SEOUL)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    t = (now.hour, now.minute)
    return (_MARKET_OPEN_H, _MARKET_OPEN_M) <= t < (_MARKET_CLOSE_H, _MARKET_CLOSE_M)


def _fetch_krx_price_sync(ticker: str) -> dict:
    """
    Fetch the latest price for a KOSDAQ/KOSPI ticker from KRX public data.

    KRX exposes stock data through the public market-data API used by
    finance.naver.com and similar sites.  We use the Naver Finance JSON
    endpoint which is freely accessible, requires no key, and returns
    real-time (during market hours) or last-close (after hours) prices.

    Endpoint:
      https://m.stock.naver.com/api/stock/{ticker}/basic
    Returns JSON with fields including: closePrice, compareToPreviousClosePrice,
      fluctuationsRatio, stockExchangeType.shortName, etc.
    """
    url = f"https://m.stock.naver.com/api/stock/{ticker}/basic"
    try:
        with httpx.Client(
            timeout=STOCK_TIMEOUT_S,
            headers={"User-Agent": "Mozilla/5.0 (compatible; bot)"},
            follow_redirects=True,
        ) as client:
            r = client.get(url)
        r.raise_for_status()
        data = r.json()

        # closePrice is the most recent trade price (live or last-close)
        price_raw = data.get("closePrice", "")
        if not price_raw:
            return {"error": f"No price data returned for {ticker}"}

        # Naver returns price as a string with commas e.g. "1,234"
        price = float(str(price_raw).replace(",", ""))

        change_raw = data.get("compareToPreviousClosePrice", "0") or "0"
        change     = float(str(change_raw).replace(",", ""))
        change_pct = data.get("fluctuationsRatio", "0") or "0"

        exchange   = data.get("stockExchangeType", {})
        exch_name  = exchange.get("shortName", "KOSDAQ") if isinstance(exchange, dict) else "KOSDAQ"

        is_open    = _is_market_open()
        label      = "현재가" if is_open else "종가 (장 마감)"  # live or last-close

        return {
            "ticker":     ticker,
            "price":      price,
            "change":     change,
            "change_pct": change_pct,
            "exchange":   exch_name,
            "label":      label,
            "is_live":    is_open,
            "currency":   "KRW",
        }

    except httpx.TimeoutException:
        msg = f"KRX/Naver timeout for {ticker}"
        log.warning(msg)
        return {"error": msg}
    except Exception as exc:
        msg = f"KRX/Naver fetch error for {ticker}: {exc}"
        log.warning(msg)
        return {"error": msg}


async def get_stock_price_krw(ticker: str) -> dict:
    """
    Async entry point for stock price.
    Returns cached result if within TTL.
    On failure, returns stale cache if available, otherwise error dict.
    """
    cached = _cache_get_stock(ticker)
    if cached:
        return cached

    result = await asyncio.to_thread(_fetch_krx_price_sync, ticker)

    if "error" in result:
        stale = _cache_get_stock_stale(ticker)
        if stale:
            # Return stale data with a note
            stale_copy = dict(stale)
            stale_copy["label"] = stale_copy.get("label", "종가") + " (캐시)"
            stale_copy["stale"] = True
            log.warning("Returning stale stock price for %s: %s", ticker, result["error"])
            return stale_copy
        return result

    _cache_set_stock(ticker, result)
    return result


def fmt_stock_price(result: dict, lang: str) -> str:
    """Format a stock price result dict into Telegram HTML."""
    if "error" in result:
        if lang == "ko":
            return f"⚠️ 주가를 불러올 수 없습니다.\n{result['error']}"
        return f"⚠️ Could not fetch stock price.\n{result['error']}"

    ticker    = result["ticker"]
    price     = result["price"]
    change    = result.get("change", 0)
    chg_pct   = result.get("change_pct", "0")
    exchange  = result.get("exchange", "KOSDAQ")
    label     = result.get("label", "가격")
    is_stale  = result.get("stale", False)

    price_fmt = f"₩{price:,.0f}"
    arrow     = "▲" if change > 0 else ("▼" if change < 0 else "—")
    chg_fmt   = f"{arrow} ₩{abs(change):,.0f} ({chg_pct}%)"
    stale_note = " ⚠️ (cached)" if is_stale else ""

    if lang == "ko":
        name = "파라택시스 코리아"
        return (
            f"📈 <b>{name}</b>  <code>{exchange}: {ticker}</code>\n"
            f"{label}: <b>{price_fmt}</b>{stale_note}\n"
            f"전일 대비: {chg_fmt}"
        )
    else:
        name = "Parataxis Korea"
        en_label = "Live price" if result.get("is_live") else "Last close"
        if is_stale:
            en_label += " (cached)"
        return (
            f"📈 <b>{name}</b>  <code>{exchange}: {ticker}</code>\n"
            f"{en_label}: <b>{price_fmt}</b>\n"
            f"Change: {chg_fmt}"
        )


# ═══════════════════════════════════════════════════════════════════════════════
# CRYPTO — unchanged from previous version
# ═══════════════════════════════════════════════════════════════════════════════

async def _coingecko_usd(coin: str) -> dict:
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


async def _upbit_krw(coin: str) -> dict:
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


async def _bithumb_krw(coin: str) -> dict:
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


async def get_price_usd(coin: str) -> dict:
    coin = coin.lower()

    cached = _cache_get(coin, "USD")
    if cached:
        return cached

    result = await _coingecko_usd(coin)

    if result.get("rate_limited"):
        log.info("Falling back to Upbit KRW for %s due to CoinGecko rate limit.", coin)
        krw_result = await _get_krw_with_cache(coin)
        if "error" not in krw_result:
            return {
                "coin":     coin,
                "price":    krw_result["price"],
                "currency": "KRW",
                "source":   krw_result["source"] + " (USD unavailable — rate limited)",
            }
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
    coin = coin.lower()
    result = await _get_krw_with_cache(coin)
    if "error" in result:
        return {"error": result["error"]}
    return result


async def get_price(coin: str, lang: str) -> dict:
    """Entry point for crypto: route to USD or KRW based on language."""
    if lang == "ko":
        return await get_price_krw(coin)
    return await get_price_usd(coin)


def fmt_price(result: dict, lang: str) -> str:
    """Format a crypto price result dict into Telegram HTML."""
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
