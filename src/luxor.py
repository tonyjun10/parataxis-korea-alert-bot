"""
luxor.py — Luxor Mining Pool API client.

All endpoints confirmed from official docs:
  GET /pool/summary/{currency_type}         → hashrate, active_miners, revenue_24h
  GET /pool/active-workers/{currency_type}  → active_workers time series
  GET /pool/revenue/{currency_type}         → revenue time series
  GET /pool/workers/{currency_type}         → total_active, workers list

Key: currency_type is a PATH param, not a query param.
subaccount_names is a repeated query param.
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import date, timedelta

import httpx

log = logging.getLogger(__name__)

LUXOR_API_KEY = os.environ.get("LUXOR_API_KEY", "")
BASE_URL      = "https://app.luxor.tech/api/v2"
SUBACCOUNTS   = ["blackcreek", "blackcreekluxos"]
CURRENCY      = "BTC"
TIMEOUT       = 15


class LuxorError(Exception):
    pass


@dataclass
class MiningStats:
    hashrate_ph:    float
    active_workers: int
    btc_today:      float
    btc_mtd:        float
    efficiency:     float  # -1 if unavailable


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {"authorization": LUXOR_API_KEY}


def _get(path: str, params: list[tuple]) -> dict | list:
    url = f"{BASE_URL}{path}"
    log.info("[luxor] GET %s  params=%s", url, params)
    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.get(url, headers=_headers(), params=params)
    if r.status_code >= 400:
        log.warning("[luxor] %s → %d: %s", path, r.status_code, r.text[:600])
        r.raise_for_status()
    data = r.json()
    log.info("[luxor] %s → 200: %s", path, str(data)[:1000])
    return data


def _sub_params(extra: list[tuple] | None = None) -> list[tuple]:
    """
    No subaccount_names filter — returns all subaccounts the API key has access to.
    The 403 errors confirmed the hardcoded names don't match what the key can see.
    We log the full response so we can identify the real subaccount names.
    """
    p = []
    if extra:
        p.extend(extra)
    return p


def _date_params(start: date, end: date, tick: str = "1d") -> list[tuple]:
    return [
        ("start_date", start.isoformat()),
        ("end_date",   end.isoformat()),
        ("tick_size",  tick),
    ]


# ── Data extraction ───────────────────────────────────────────────────────────

def _find_list(data, *wrapper_keys) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in wrapper_keys:
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


def _sum_field(records: list, *keys: str) -> float:
    total = 0.0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        for k in keys:
            v = rec.get(k)
            if v is not None:
                try:
                    total += float(v)
                    break
                except (TypeError, ValueError):
                    pass
    return total


def _latest_field(data, *keys: str) -> float:
    """Get a field from the most recent record or flat dict."""
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
    records = _find_list(data, "active_workers", "revenue", "subaccounts",
                         "workers", "data", "items")
    for rec in reversed(records):
        if not isinstance(rec, dict):
            continue
        for k in keys:
            v = rec.get(k)
            if v is not None:
                try:
                    return float(v)
                except (TypeError, ValueError):
                    pass
    return 0.0


# ── Core fetch ────────────────────────────────────────────────────────────────

def _fetch_sync() -> MiningStats:
    if not LUXOR_API_KEY:
        raise LuxorError("LUXOR_API_KEY is not set.")

    today       = date.today()
    yesterday   = today - timedelta(days=1)
    month_start = today.replace(day=1)

    # ── DIAGNOSTIC: try /pool/workers/BTC with no filter to discover subaccount names ──
    try:
        diag = _get(f"/pool/workers/{CURRENCY}", [])
        log.info("[luxor] DIAGNOSTIC workers (no filter): %s", str(diag)[:1500])
    except Exception as e:
        log.warning("[luxor] DIAGNOSTIC workers failed: %s", e)

    # ── /pool/summary/BTC — hashrate + active miners + 24h revenue ────────
    hr_ph      = 0.0
    efficiency = -1.0
    btc_today  = 0.0
    try:
        data = _get(f"/pool/summary/{CURRENCY}", _sub_params())
        # hashrate_5m is in H/s
        raw_hr = _latest_field(data, "hashrate_5m", "hashrate_1h", "hashrate_24h")
        hr_ph  = raw_hr / 1e15 if raw_hr else 0.0
        raw_eff = _latest_field(data, "efficiency_24h", "efficiency_1h", "efficiency_5m")
        if raw_eff:
            efficiency = raw_eff * 100 if raw_eff <= 1.0 else raw_eff
        # revenue_24h is a list of {currency_type, revenue_type, revenue}
        rev_list  = _find_list(data.get("revenue_24h", []) if isinstance(data, dict) else [],
                               "revenue")
        if not rev_list and isinstance(data, dict):
            rev_list = _find_list(data, "revenue_24h", "revenue")
        btc_today = _sum_field(rev_list, "revenue")
        if btc_today == 0.0:
            # try direct key on summary dict
            btc_today = _latest_field(data, "revenue_24h", "revenue")
    except Exception as e:
        log.warning("[luxor] summary failed: %s", e)

    # ── /pool/active-workers/BTC — worker count ────────────────────────────
    workers = 0
    try:
        params = _date_params(yesterday, today, "1h") + _sub_params()
        data   = _get(f"/pool/active-workers/{CURRENCY}", params)
        # Response: {subaccounts:[...], active_workers:[{date_time, active_workers}], pagination}
        aw_list  = _find_list(data, "active_workers")
        latest   = aw_list[-1] if aw_list else {}
        workers  = int(float(latest.get("active_workers", 0)))
    except Exception as e:
        log.warning("[luxor] active-workers failed: %s", e)

    # ── /pool/revenue/BTC — MTD revenue ───────────────────────────────────
    btc_mtd = 0.0
    try:
        params  = _date_params(month_start, today, "1d") + _sub_params()
        data    = _get(f"/pool/revenue/{CURRENCY}", params)
        # Response: {subaccounts:[...], revenue:[{date_time, revenue:{currency_type,revenue_type,revenue}}]}
        rev_list = _find_list(data, "revenue")
        for entry in rev_list:
            if not isinstance(entry, dict):
                continue
            inner = entry.get("revenue", {})
            if isinstance(inner, dict):
                try:
                    btc_mtd += float(inner.get("revenue", 0) or 0)
                except (TypeError, ValueError):
                    pass
            elif isinstance(inner, (int, float)):
                btc_mtd += float(inner)
    except Exception as e:
        log.warning("[luxor] revenue MTD failed: %s", e)

    log.info("[luxor] final → %.6f PH/s  %d workers  today=%.8f  mtd=%.8f  eff=%s",
             hr_ph, workers, btc_today, btc_mtd,
             f"{efficiency:.1f}%" if efficiency >= 0 else "n/a")

    return MiningStats(
        hashrate_ph    = hr_ph,
        active_workers = workers,
        btc_today      = btc_today,
        btc_mtd        = btc_mtd,
        efficiency     = efficiency,
    )


async def get_mining_stats() -> MiningStats:
    return await asyncio.to_thread(_fetch_sync)


# ── Formatting ────────────────────────────────────────────────────────────────

def fmt_mining_stats(stats: MiningStats, lang: str = "en") -> str:
    eff_str = f"{stats.efficiency:.1f}%" if stats.efficiency >= 0 else "N/A"
    if lang == "ko":
        return (
            "⚡ <b>채굴 현황</b>\n\n"
            f"플릿 해시레이트: <b>{stats.hashrate_ph:.4f} PH/s</b>\n"
            f"활성 워커: <b>{stats.active_workers}</b>\n"
            f"오늘 채굴 BTC: <b>{stats.btc_today:.8f} BTC</b>\n"
            f"이번 달 채굴 BTC: <b>{stats.btc_mtd:.8f} BTC</b>\n"
            f"효율: <b>{eff_str}</b>"
        )
    return (
        "⚡ <b>Mining Update</b>\n\n"
        f"Fleet Hashrate: <b>{stats.hashrate_ph:.4f} PH/s</b>\n"
        f"Active Workers: <b>{stats.active_workers}</b>\n"
        f"BTC Mined Today: <b>{stats.btc_today:.8f} BTC</b>\n"
        f"BTC Mined MTD: <b>{stats.btc_mtd:.8f} BTC</b>\n"
        f"Efficiency: <b>{eff_str}</b>"
    )
