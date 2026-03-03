"""
prices.py — Cryptocurrency price fetching.

USD: CoinGecko public API (no key required)
KRW: Upbit (primary) → Bithumb (fallback)
"""

import asyncio
import logging

import httpx

log = logging.getLogger(__name__)

TIMEOUT = 10  # seconds

# CoinGecko coin IDs
_COINGECKO_IDS = {
    "btc": "bitcoin",
    "eth": "ethereum",
    "xrp": "ripple",
}

# Upbit market codes
_UPBIT_MARKETS = {
    "btc": "KRW-BTC",
    "eth": "KRW-ETH",
    "xrp": "KRW-XRP",
}

# Bithumb market codes
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


async def get_price_usd(coin: str) -> dict:
    """Fetch USD price from CoinGecko. Returns {coin, price, currency, source}."""
    coin_id = _COINGECKO_IDS.get(coin.lower())
    if not coin_id:
        return {"error": f"Unknown coin: {coin}"}
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id}&vs_currencies=usd"
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.get(url)
        r.raise_for_status()
        price = r.json()[coin_id]["usd"]
        return {"coin": coin.lower(), "price": price, "currency": "USD", "source": "CoinGecko"}
    except Exception as e:
        log.warning("CoinGecko fetch failed for %s: %s", coin, e)
        return {"error": str(e)}


async def _upbit_krw(coin: str) -> float | None:
    market = _UPBIT_MARKETS.get(coin.lower())
    if not market:
        return None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.get(
                "https://api.upbit.com/v1/ticker",
                params={"markets": market}
            )
        r.raise_for_status()
        data = r.json()
        if data:
            return float(data[0]["trade_price"])
    except Exception as e:
        log.warning("Upbit fetch failed for %s: %s", coin, e)
    return None


async def _bithumb_krw(coin: str) -> float | None:
    code = _BITHUMB_COINS.get(coin.lower())
    if not code:
        return None
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.get(
                f"https://api.bithumb.com/public/ticker/{code}_KRW"
            )
        r.raise_for_status()
        data = r.json()
        if data.get("status") == "0000":
            return float(data["data"]["closing_price"])
    except Exception as e:
        log.warning("Bithumb fetch failed for %s: %s", coin, e)
    return None


async def get_price_krw(coin: str) -> dict:
    """Fetch KRW price: Upbit first, Bithumb fallback."""
    price = await _upbit_krw(coin)
    source = "Upbit"
    if price is None:
        price = await _bithumb_krw(coin)
        source = "Bithumb"
    if price is None:
        return {"error": f"Could not fetch KRW price for {coin}"}
    return {"coin": coin.lower(), "price": price, "currency": "KRW", "source": source}


async def get_price(coin: str, lang: str) -> dict:
    """Get price in the correct currency for the user's language."""
    if lang == "ko":
        return await get_price_krw(coin)
    else:
        return await get_price_usd(coin)


def fmt_price(result: dict, lang: str) -> str:
    """Format a price result dict into a Telegram-ready string."""
    if "error" in result:
        return f"⚠️ {result['error']}"
    coin     = result["coin"]
    price    = result["price"]
    currency = result["currency"]
    source   = result.get("source", "")
    name     = _COIN_NAMES.get(coin, {}).get(lang, coin.upper())

    if currency == "KRW":
        formatted = f"₩{price:,.0f}"
    else:
        formatted = f"${price:,.2f}"

    if lang == "ko":
        return f"💰 <b>{name}</b>\n현재가: <b>{formatted}</b>\n출처: {source}"
    else:
        return f"💰 <b>{name}</b>\nPrice: <b>{formatted}</b>\nSource: {source}"
