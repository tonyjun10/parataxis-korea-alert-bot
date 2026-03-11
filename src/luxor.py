"""
luxor.py — Luxor Mining Pool API client.

Confirmed from API testing:
  - /pool/hashrate-efficiency/BTC EXISTS (needs start_date, end_date, tick_size)
  - subaccount_names must be passed as REPEATED params, not comma-joined
  - /pool/revenue, /pool/summary, /pool/workers, /pool/pool-stats do NOT exist

Trying subaccount-level endpoints for workers/revenue which are likely
under /subaccounts/ prefix based on the docs sidebar structure.
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
SUBACCOUNTS   = ["blackcreek", "blackcreekluxoos"]
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


def _get(path: str, params: list[tuple]) -> dict | list:
    """
    params is a list of (key, value) tuples to support repeated keys.
    e.g. [("subaccount_names", "blackcreek"), ("subaccount_names", "blackcreekluxoos")]
    """
    url = f"{BASE_URL}{path}"
    log.info("[luxor] GET %s  params=%s", url, params)
    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.get(url, headers=_headers(), params=params)
    if r.status_code >= 400:
        log.warning("[luxor] %s → %d body: %s", path, r.status_code, r.text[:600])
        r.raise_for_status()
    data = r.json()
    log.info("[luxor] %s → 200: %s", path, str(data)[:800])
    return data


def _subaccount_params(extra: list[tuple] | None = None) -> list[tuple]:
    """Subaccount names as repeated params — required by Luxor API."""
    params = [("subaccount_names", s) for s in SUBACCOUNTS]
    if extra:
        params.extend(extra)
    return params


def _date_params(start: date, end: date, tick: str = "1d") -> list[tuple]:
    return [
        ("start_date", start.isoformat()),
        ("end_date",   end.isoformat()),
        ("tick_size",  tick),
    ]


# ── Extraction helpers ────────────────────────────────────────────────────────

def _find_list(data: dict | list) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("hashrate_efficiency", "data", "result", "workers",
                    "revenue", "items", "subaccounts"):
            v = data.get(key)
            if isinstance(v, list):
                return v
    return []


def _sum_key(data: dict | list, *keys: str) -> float:
    records = _find_list(data)
    if not records:
        # flat dict
        if isinstance(data, dict):
            for k in keys:
                v = data.get(k)
                if v is not None:
                    try:
                        return float(v)
                    except (TypeError, ValueError):
                        pass
        return 0.0
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


def _latest_key(data: dict | list, *keys: str) -> float:
    records = _find_list(data)
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


# ── Fetch ─────────────────────────────────────────────────────────────────────

def _fetch_sync() -> MiningStats:
    if not LUXOR_API_KEY:
        raise LuxorError("LUXOR_API_KEY is not set.")

    today       = date.today()
    yesterday   = today - timedelta(days=1)
    month_start = today.replace(day=1)

    # ── Hashrate + efficiency (confirmed working endpoint) ─────────────────
    hr_ph      = 0.0
    efficiency = -1.0
    try:
        params = _date_params(yesterday, today, "1h") + _subaccount_params()
        data   = _get(f"/pool/hashrate-efficiency/{CURRENCY}", params)
        raw_hr = _latest_key(data, "hashrate", "avg_hashrate", "currentHashrate")
        hr_ph  = raw_hr / 1e15 if raw_hr else 0.0
        raw_eff = _latest_key(data, "efficiency", "hashrate_efficiency", "eff")
        if raw_eff:
            efficiency = raw_eff * 100 if raw_eff <= 1.0 else raw_eff
    except Exception as e:
        log.warning("[luxor] hashrate-efficiency failed: %s", e)

    # ── Workers — try subaccount-level and pool-level paths ───────────────
    workers = 0
    worker_paths = [
        "/pool/get-active-workers",
        "/pool/workers",
        "/subaccounts/workers",
        "/subaccounts/active-workers",
    ]
    for path in worker_paths:
        try:
            data    = _get(path, _subaccount_params())
            workers = int(_sum_key(data, "active_workers", "activeWorkers",
                                   "workers", "worker_count") or 0)
            break
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                continue
            log.warning("[luxor] workers %s failed: %s", path, e)
            break
        except Exception as e:
            log.warning("[luxor] workers %s failed: %s", path, e)
            break

    # ── Revenue — try various paths with date params ───────────────────────
    btc_today = 0.0
    btc_mtd   = 0.0
    revenue_paths = [
        "/pool/get-revenue",
        "/subaccounts/revenue",
        "/pool/hashrate-revenue",
        "/pool/revenue-history",
    ]
    for path in revenue_paths:
        try:
            today_params = _date_params(yesterday, today) + _subaccount_params()
            mtd_params   = _date_params(month_start, today) + _subaccount_params()
            rev_today    = _get(path, today_params)
            btc_today    = _sum_key(rev_today, "revenue", "amount", "btc_amount",
                                    "total_revenue", "daily_revenue")
            rev_mtd      = _get(path, mtd_params)
            btc_mtd      = _sum_key(rev_mtd, "revenue", "amount", "btc_amount",
                                    "total_revenue", "monthly_revenue")
            break
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                continue
            log.warning("[luxor] revenue %s failed: %s", path, e)
            break
        except Exception as e:
            log.warning("[luxor] revenue %s failed: %s", path, e)
            break

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
