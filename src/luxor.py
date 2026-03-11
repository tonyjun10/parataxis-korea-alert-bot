"""
luxor.py — Luxor Mining Pool API client.

Confirmed working endpoints (from API testing):
  GET /pool/hashrate-efficiency/{currency_type}
    Required: start_date, end_date, tick_size
    Optional: subaccount_names

  GET /pool/workers          (to be confirmed)
  GET /pool/pool-stats       (to be confirmed)
  GET /pool/hashrate         (to be confirmed)

Strategy: use hashrate-efficiency for hashrate + efficiency data.
For workers/revenue, try multiple candidate endpoint names and log
the raw response so we can identify correct keys on first run.
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
SUBACCOUNTS   = "blackcreek,blackcreekluxoos"   # comma-separated, both at once
TIMEOUT       = 15
CURRENCY      = "BTC"


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


def _get(path: str, params: dict) -> dict | list:
    url = f"{BASE_URL}{path}"
    log.info("[luxor] GET %s  params=%s", url, {k: v for k, v in params.items() if k != "authorization"})
    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.get(url, headers=_headers(), params=params)
    if r.status_code >= 400:
        log.warning("[luxor] %s → %d body: %s", path, r.status_code, r.text[:600])
        r.raise_for_status()
    data = r.json()
    log.info("[luxor] %s → 200 body: %s", path, str(data)[:800])
    return data


def _today_params(tick: str = "1d", extra: dict | None = None) -> dict:
    """Params for a query spanning today (MTD = first of month to today)."""
    today     = date.today()
    yesterday = today - timedelta(days=1)
    params = {
        "start_date":      yesterday.isoformat(),
        "end_date":        today.isoformat(),
        "tick_size":       tick,
        "subaccount_names": SUBACCOUNTS,
    }
    if extra:
        params.update(extra)
    return params


def _mtd_params(tick: str = "1d") -> dict:
    today     = date.today()
    month_start = today.replace(day=1)
    return {
        "start_date":      month_start.isoformat(),
        "end_date":        today.isoformat(),
        "tick_size":       tick,
        "subaccount_names": SUBACCOUNTS,
    }


# ── Data extraction ───────────────────────────────────────────────────────────

def _sum_field(data: dict | list, *keys: str) -> float:
    """
    Sum a numeric field across a list of records, or extract from a dict.
    Handles both list responses and dict responses with a 'data' wrapper.
    """
    records = []
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        for wrapper in ("hashrate_efficiency", "data", "result", "workers",
                        "pool_stats", "revenue", "items"):
            inner = data.get(wrapper)
            if isinstance(inner, list):
                records = inner
                break
        if not records:
            # flat dict — try direct keys
            for key in keys:
                val = data.get(key)
                if val is not None:
                    try:
                        return float(val)
                    except (TypeError, ValueError):
                        pass

    total = 0.0
    for rec in records:
        if not isinstance(rec, dict):
            continue
        for key in keys:
            val = rec.get(key)
            if val is not None:
                try:
                    total += float(val)
                    break
                except (TypeError, ValueError):
                    pass
    return total


def _latest_field(data: dict | list, *keys: str) -> float:
    """Return the value from the most recent record (last in list)."""
    records = []
    if isinstance(data, list):
        records = data
    elif isinstance(data, dict):
        for wrapper in ("hashrate_efficiency", "data", "result", "workers",
                        "pool_stats", "revenue", "items"):
            inner = data.get(wrapper)
            if isinstance(inner, list):
                records = inner
                break

    # Try last record first (most recent), then first
    for rec in reversed(records):
        if not isinstance(rec, dict):
            continue
        for key in keys:
            val = rec.get(key)
            if val is not None:
                try:
                    return float(val)
                except (TypeError, ValueError):
                    pass
    return 0.0


# ── Fetch functions ───────────────────────────────────────────────────────────

def _fetch_hashrate_efficiency_sync() -> dict | list:
    """Confirmed working endpoint. Returns hashrate + efficiency history."""
    params = _today_params(tick="1h")
    return _get(f"/pool/hashrate-efficiency/{CURRENCY}", params)


def _fetch_workers_sync() -> dict | list:
    """Try known worker endpoint names."""
    params = {"subaccount_names": SUBACCOUNTS}
    for path in ("/pool/workers", "/pool/active-workers", "/pool/pool-stats"):
        try:
            return _get(path, params)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                continue
            raise
    return {}


def _fetch_revenue_today_sync() -> dict | list:
    """Try to get today's revenue."""
    for path in ("/pool/revenue", "/pool/pool-stats", "/pool/summary"):
        try:
            return _get(path, _today_params())
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                continue
            raise
    return {}


def _fetch_revenue_mtd_sync() -> dict | list:
    """Try to get MTD revenue."""
    for path in ("/pool/revenue", "/pool/pool-stats", "/pool/summary"):
        try:
            return _get(path, _mtd_params())
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                continue
            raise
    return {}


# ── Main aggregation ──────────────────────────────────────────────────────────

def _fetch_all_sync() -> MiningStats:
    if not LUXOR_API_KEY:
        raise LuxorError("LUXOR_API_KEY is not set.")

    # ── Hashrate + efficiency (confirmed working) ──────────────────────────
    hr_ph      = 0.0
    efficiency = -1.0
    try:
        eff_data = _fetch_hashrate_efficiency_sync()
        # hashrate field is in H/s — convert to PH/s
        raw_hr = _latest_field(eff_data, "hashrate", "avg_hashrate", "currentHashrate")
        hr_ph  = raw_hr / 1e15 if raw_hr else 0.0
        raw_eff = _latest_field(eff_data, "efficiency", "hashrate_efficiency", "eff")
        if raw_eff:
            efficiency = raw_eff * 100 if raw_eff <= 1.0 else raw_eff
    except Exception as e:
        log.warning("[luxor] hashrate-efficiency fetch failed: %s", e)

    # ── Workers ───────────────────────────────────────────────────────────
    workers = 0
    try:
        w_data  = _fetch_workers_sync()
        workers = int(_latest_field(w_data, "active_workers", "activeWorkers",
                                    "workers", "worker_count", "workerCount") or 0)
    except Exception as e:
        log.warning("[luxor] workers fetch failed: %s", e)

    # ── Revenue today ─────────────────────────────────────────────────────
    btc_today = 0.0
    try:
        rev_today = _fetch_revenue_today_sync()
        btc_today = _sum_field(rev_today, "revenue", "amount", "btc_amount",
                               "total_revenue", "revenueToday", "daily_revenue")
    except Exception as e:
        log.warning("[luxor] revenue today fetch failed: %s", e)

    # ── Revenue MTD ───────────────────────────────────────────────────────
    btc_mtd = 0.0
    try:
        rev_mtd = _fetch_revenue_mtd_sync()
        btc_mtd = _sum_field(rev_mtd, "revenue", "amount", "btc_amount",
                              "total_revenue", "revenueMTD", "monthly_revenue")
    except Exception as e:
        log.warning("[luxor] revenue MTD fetch failed: %s", e)

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
    return await asyncio.to_thread(_fetch_all_sync)


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
