"""
luxor.py — Luxor Mining Pool API client.
"""

import asyncio
import logging
import os
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

LUXOR_API_KEY = os.environ.get("LUXOR_API_KEY", "")
BASE_URL      = "https://app.luxor.tech/api/v2"
SUBACCOUNTS   = ["blackcreek", "blackcreekluxoos"]
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

def _get(path: str, params: dict | None = None) -> dict:
    url = f"{BASE_URL}{path}"
    log.info("[luxor] GET %s params=%s", url, params)
    headers = {
        "authorization": LUXOR_API_KEY,
        "x-lux-api-key": LUXOR_API_KEY,      # some Luxor endpoints use this
    }
    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.get(url, headers=headers, params=params or {})

    # Log response body on failure so we can see the real error
    if r.status_code >= 400:
        log.warning("[luxor] %s → %d: %s", url, r.status_code, r.text[:500])
        r.raise_for_status()

    return r.json()


# ── Flat extraction ───────────────────────────────────────────────────────────

def _search(data: dict, keys: list[str]):
    for key in keys:
        if key in data and data[key] is not None:
            return data[key]
    for wrapper in ("data", "result"):
        inner = data.get(wrapper)
        if isinstance(inner, dict):
            for key in keys:
                if key in inner and inner[key] is not None:
                    return inner[key]
    return None


def _extract_hashrate(data: dict) -> float:
    val = _search(data, ["hashrate", "currentHashrate", "current_hashrate",
                         "avgHashrate", "avg_hashrate", "hr"])
    try:
        return float(val) / 1e15 if val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _extract_workers(data: dict) -> int:
    val = _search(data, ["activeWorkers", "active_workers", "workers",
                         "workerCount", "worker_count"])
    try:
        return int(val) if val is not None else 0
    except (TypeError, ValueError):
        return 0


def _extract_today(data: dict) -> float:
    val = _search(data, ["revenueToday", "revenue_today", "dailyRevenue",
                         "daily_revenue", "todayRevenue", "today_revenue"])
    try:
        return float(val) if val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _extract_mtd(data: dict) -> float:
    val = _search(data, ["revenueMTD", "revenue_mtd", "monthlyRevenue",
                         "monthly_revenue", "mtdRevenue", "mtd_revenue"])
    try:
        return float(val) if val is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _extract_efficiency(data: dict) -> float:
    val = _search(data, ["efficiency", "hashrateEfficiency", "hashrate_efficiency", "eff"])
    if val is None:
        return -1.0
    try:
        v = float(val)
        return v * 100 if v <= 1.0 else v
    except (TypeError, ValueError):
        return -1.0


# ── Per-subaccount fetch ──────────────────────────────────────────────────────

def _fetch_subaccount_sync(subaccount: str) -> tuple[float, int, float, float, float]:
    params = {"subaccount_names": subaccount}

    summary = {}
    try:
        summary = _get("/pool/summary", params)
        log.info("[luxor/%s] summary: %s", subaccount, summary)
    except Exception as e:
        log.warning("[luxor/%s] summary failed: %s", subaccount, e)

    revenue = {}
    try:
        revenue = _get("/pool/revenue", params)
        log.info("[luxor/%s] revenue: %s", subaccount, revenue)
    except Exception as e:
        log.warning("[luxor/%s] revenue failed: %s", subaccount, e)

    eff_data = {}
    try:
        eff_data = _get("/pool/hashrate-efficiency/BTC", params)
        log.info("[luxor/%s] efficiency: %s", subaccount, eff_data)
    except Exception as e:
        log.warning("[luxor/%s] efficiency failed: %s", subaccount, e)

    hr      = _extract_hashrate(summary)
    workers = _extract_workers(summary)
    today   = _extract_today(revenue)
    mtd     = _extract_mtd(revenue)
    eff     = _extract_efficiency(eff_data)

    log.info("[luxor/%s] parsed → %.6f PH/s  %d workers  today=%.8f  mtd=%.8f  eff=%s",
             subaccount, hr, workers, today, mtd,
             f"{eff:.1f}%" if eff >= 0 else "n/a")
    return hr, workers, today, mtd, eff


def _fetch_all_sync() -> MiningStats:
    if not LUXOR_API_KEY:
        raise LuxorError("LUXOR_API_KEY is not set.")

    total_hr, total_w, total_today, total_mtd = 0.0, 0, 0.0, 0.0
    eff_values: list[float] = []

    for sub in SUBACCOUNTS:
        hr, w, today, mtd, eff = _fetch_subaccount_sync(sub)
        total_hr    += hr
        total_w     += w
        total_today += today
        total_mtd   += mtd
        if eff >= 0:
            eff_values.append(eff)

    return MiningStats(
        hashrate_ph    = total_hr,
        active_workers = total_w,
        btc_today      = total_today,
        btc_mtd        = total_mtd,
        efficiency     = sum(eff_values) / len(eff_values) if eff_values else -1.0,
    )


# ── Public async entry point ──────────────────────────────────────────────────

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
