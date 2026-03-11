"""
luxor.py — Luxor Mining Pool API client.

Fetches stats for both subaccounts (blackcreek, blackcreekluxoos) and
aggregates them into a single MiningStats object.
"""

import asyncio
import logging
import os
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

LUXOR_API_KEY  = os.environ.get("LUXOR_API_KEY", "")
BASE_URL       = "https://app.luxor.tech/api/v2"
SUBACCOUNTS    = ["blackcreek", "blackcreekluxoos"]
TIMEOUT        = 15


class LuxorError(Exception):
    pass


@dataclass
class MiningStats:
    hashrate_ph:   float   # fleet hashrate in PH/s
    active_workers: int    # total active workers
    btc_today:     float   # BTC mined today
    btc_mtd:       float   # BTC mined month-to-date
    efficiency:    float   # efficiency % (0–100), -1 if unavailable


# ── Sync helpers ──────────────────────────────────────────────────────────────

def _headers() -> dict:
    return {"authorization": LUXOR_API_KEY}


def _get(path: str, params: dict | None = None) -> dict:
    url = f"{BASE_URL}{path}"
    with httpx.Client(timeout=TIMEOUT) as client:
        r = client.get(url, headers=_headers(), params=params or {})
    r.raise_for_status()
    return r.json()


def _fetch_summary_sync(subaccount: str) -> dict:
    """GET /v2/pool/summary for one subaccount."""
    try:
        return _get("/pool/summary", {"subaccount_names": subaccount})
    except Exception as e:
        log.warning("[luxor] summary failed for %s: %s", subaccount, e)
        return {}


def _fetch_revenue_sync(subaccount: str) -> dict:
    """GET /v2/pool/revenue for one subaccount."""
    try:
        return _get("/pool/revenue", {"subaccount_names": subaccount})
    except Exception as e:
        log.warning("[luxor] revenue failed for %s: %s", subaccount, e)
        return {}


def _fetch_efficiency_sync(subaccount: str) -> dict:
    """GET /v2/pool/hashrate-efficiency/BTC for one subaccount."""
    try:
        return _get("/pool/hashrate-efficiency/BTC", {"subaccount_names": subaccount})
    except Exception as e:
        log.warning("[luxor] efficiency failed for %s: %s", subaccount, e)
        return {}


def _extract_hashrate(data: dict) -> float:
    """
    Pull hashrate in PH/s from a summary response.
    Luxor returns hashrate in H/s — divide by 1e15 to get PH/s.
    Tries common key names across API versions.
    """
    for key in ("hashrate", "currentHashrate", "current_hashrate", "avgHashrate"):
        val = data.get(key)
        if val is not None:
            try:
                return float(val) / 1e15
            except (TypeError, ValueError):
                pass
    # Some responses nest under 'data'
    inner = data.get("data") or data.get("result") or {}
    if isinstance(inner, dict):
        return _extract_hashrate(inner)
    return 0.0


def _extract_workers(data: dict) -> int:
    for key in ("activeWorkers", "active_workers", "workers", "workerCount"):
        val = data.get(key)
        if val is not None:
            try:
                return int(val)
            except (TypeError, ValueError):
                pass
    inner = data.get("data") or data.get("result") or {}
    if isinstance(inner, dict):
        return _extract_workers(inner)
    return 0


def _extract_revenue(data: dict, key_today: str, key_mtd: str) -> tuple[float, float]:
    """Return (btc_today, btc_mtd) from a revenue response."""
    today = 0.0
    mtd   = 0.0
    for d in [data, data.get("data", {}), data.get("result", {})]:
        if not isinstance(d, dict):
            continue
        for k in (key_today, "revenueToday", "revenue_today", "dailyRevenue", "daily_revenue"):
            v = d.get(k)
            if v is not None:
                try:
                    today = float(v)
                    break
                except (TypeError, ValueError):
                    pass
        for k in (key_mtd, "revenueMTD", "revenue_mtd", "monthlyRevenue", "monthly_revenue"):
            v = d.get(k)
            if v is not None:
                try:
                    mtd = float(v)
                    break
                except (TypeError, ValueError):
                    pass
        if today or mtd:
            break
    return today, mtd


def _extract_efficiency(data: dict) -> float:
    for key in ("efficiency", "hashrateEfficiency", "hashrate_efficiency"):
        val = data.get(key)
        if val is not None:
            try:
                v = float(val)
                # Normalise: if value looks like a ratio (0–1) convert to %
                return v * 100 if v <= 1.0 else v
            except (TypeError, ValueError):
                pass
    inner = data.get("data") or data.get("result") or {}
    if isinstance(inner, dict):
        return _extract_efficiency(inner)
    return -1.0


def _fetch_all_sync() -> MiningStats:
    """Fetch and aggregate stats across all subaccounts."""
    if not LUXOR_API_KEY:
        raise LuxorError("LUXOR_API_KEY is not set.")

    total_hashrate  = 0.0
    total_workers   = 0
    total_today     = 0.0
    total_mtd       = 0.0
    eff_values: list[float] = []

    for sub in SUBACCOUNTS:
        summary    = _fetch_summary_sync(sub)
        revenue    = _fetch_revenue_sync(sub)
        efficiency = _fetch_efficiency_sync(sub)

        total_hashrate += _extract_hashrate(summary)
        total_workers  += _extract_workers(summary)

        today, mtd = _extract_revenue(revenue, "revenueToday", "revenueMTD")
        total_today += today
        total_mtd   += mtd

        eff = _extract_efficiency(efficiency)
        if eff >= 0:
            eff_values.append(eff)

        log.info(
            "[luxor/%s] hashrate=%.4f PH/s workers=%d today=%.8f mtd=%.8f eff=%s",
            sub, _extract_hashrate(summary), _extract_workers(summary),
            today, mtd, f"{eff:.1f}%" if eff >= 0 else "n/a",
        )

    avg_eff = sum(eff_values) / len(eff_values) if eff_values else -1.0

    return MiningStats(
        hashrate_ph    = total_hashrate,
        active_workers = total_workers,
        btc_today      = total_today,
        btc_mtd        = total_mtd,
        efficiency     = avg_eff,
    )


# ── Public async entry point ──────────────────────────────────────────────────

async def get_mining_stats() -> MiningStats:
    """Async wrapper — never blocks the event loop."""
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
