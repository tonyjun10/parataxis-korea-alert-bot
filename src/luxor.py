"""
luxor.py — Luxor Mining Pool API client.

Fetches stats for both subaccounts (blackcreek, blackcreekluxoos) and
aggregates them into a single MiningStats object.

Extraction is flat (no recursion) — logs the full raw response so key
names can be identified on first run and hardcoded if needed.
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
    hashrate_ph:    float  # fleet hashrate in PH/s
    active_workers: int    # total active workers
    btc_today:      float  # BTC mined today
    btc_mtd:        float  # BTC mined month-to-date
    efficiency:     float  # efficiency % (0–100), -1 if unavailable


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {"authorization": LUXOR_API_KEY}


def _get(path: str, params: dict | None = None) -> dict:
    url = f"{BASE_URL}{path}"
    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.get(url, headers=_headers(), params=params or {})
    r.raise_for_status()
    return r.json()


# ── Flat extraction helpers ───────────────────────────────────────────────────
# No recursion — search top-level and one level of nesting only.

def _search(data: dict, keys: list[str]) -> float | int | None:
    """Search top-level keys, then one level inside 'data' / 'result'."""
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
    # Luxor returns H/s — convert to PH/s
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
    val = _search(data, ["efficiency", "hashrateEfficiency", "hashrate_efficiency",
                         "eff"])
    if val is None:
        return -1.0
    try:
        v = float(val)
        return v * 100 if v <= 1.0 else v
    except (TypeError, ValueError):
        return -1.0


# ── Per-subaccount fetch ──────────────────────────────────────────────────────

def _fetch_subaccount_sync(subaccount: str) -> tuple[float, int, float, float, float]:
    """Returns (hashrate_ph, workers, btc_today, btc_mtd, efficiency)."""
    params = {"subaccount_names": subaccount}

    summary = {}
    try:
        summary = _get("/pool/summary", params)
        log.info("[luxor/%s] summary raw: %s", subaccount, summary)
    except Exception as e:
        log.warning("[luxor/%s] summary failed: %s", subaccount, e)

    revenue = {}
    try:
        revenue = _get("/pool/revenue", params)
        log.info("[luxor/%s] revenue raw: %s", subaccount, revenue)
    except Exception as e:
        log.warning("[luxor/%s] revenue failed: %s", subaccount, e)

    efficiency_data = {}
    try:
        efficiency_data = _get("/pool/hashrate-efficiency/BTC", params)
        log.info("[luxor/%s] efficiency raw: %s", subaccount, efficiency_data)
    except Exception as e:
        log.warning("[luxor/%s] efficiency failed: %s", subaccount, e)

    return (
        _extract_hashrate(summary),
        _extract_workers(summary),
        _extract_today(revenue),
        _extract_mtd(revenue),
        _extract_efficiency(efficiency_data),
    )


def _fetch_all_sync() -> MiningStats:
    if not LUXOR_API_KEY:
        raise LuxorError("LUXOR_API_KEY is not set.")

    total_hashrate  = 0.0
    total_workers   = 0
    total_today     = 0.0
    total_mtd       = 0.0
    eff_values: list[float] = []

    for sub in SUBACCOUNTS:
        hr, workers, today, mtd, eff = _fetch_subaccount_sync(sub)
        total_hashrate += hr
        total_workers  += workers
        total_today    += today
        total_mtd      += mtd
        if eff >= 0:
            eff_values.append(eff)
        log.info(
            "[luxor/%s] parsed → hashrate=%.6f PH/s workers=%d today=%.8f mtd=%.8f eff=%s",
            sub, hr, workers, today, mtd,
            f"{eff:.1f}%" if eff >= 0 else "n/a",
        )

    return MiningStats(
        hashrate_ph    = total_hashrate,
        active_workers = total_workers,
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
