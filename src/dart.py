"""
dart.py — DART OpenAPI integration.

Changes in this version:
  - Default limit restored to 5 (was incorrectly changed to 10).
  - Corp code cache is now downloaded eagerly at import time via
    warm_up_corp_codes(), which main.py calls once at startup inside
    asyncio.to_thread(). This means the cache is ready before the first
    user request arrives, eliminating the 10–15 minute cold-start lag.
  - _download_corp_codes() and _load_corp_codes() are both synchronous
    and safe to call from a thread. get_corp_code() remains synchronous
    and callable from any thread.
  - All other logic (overrides, name variants, timeout, async wrapper)
    is unchanged.
"""

import asyncio
import io
import logging
import os
import re
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)
SEOUL        = ZoneInfo("Asia/Seoul")
DART_API_KEY = os.environ.get("DART_API_KEY", "")

CORP_CODE_CACHE = Path("data/corp_codes.xml")

# ── Corp code overrides ────────────────────────────────────────────────────────
# Fill in the exact 8-digit DART corp_code if you know it.
# Leave as "" to fall through to name-search lookup.
CORP_CODE_OVERRIDES: dict[str, str] = {
    "parataxis": "",   # e.g. "00123456" — fill in once confirmed
    "bitmax":    "",
    "bitplanet": "",
}

CORP_NAME_VARIANTS: dict[str, list[str]] = {
    "parataxis": ["파라택시스", "parataxis", "para taxis"],
    "bitmax":    ["비트맥스", "bitmax"],
    "bitplanet": ["비트플래닛", "bitplanet"],
}

_corp_code_map: dict[str, str] = {}
_cache_loaded:  bool            = False


def _normalise(s: str) -> str:
    return re.sub(r"\s+", "", s.lower())


# ── Cache load / download ──────────────────────────────────────────────────────

def _download_corp_codes() -> bool:
    """
    Download and unzip the DART corp code XML.
    Synchronous — always call via asyncio.to_thread() or at startup.
    Returns True on success.
    """
    url = "https://opendart.fss.or.kr/api/corpCode.xml"
    try:
        CORP_CODE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=30) as client:
            r = client.get(url, params={"crtfc_key": DART_API_KEY})
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            xml_bytes = z.read(z.namelist()[0])
        CORP_CODE_CACHE.write_bytes(xml_bytes)
        log.info("Corp code XML downloaded (%d bytes).", len(xml_bytes))
        return True
    except Exception as e:
        log.error("Corp code download failed: %s", e)
        return False


def _load_corp_codes() -> bool:
    """
    Parse the cached corp code XML into _corp_code_map.
    Downloads the file first if it does not exist.
    Synchronous — always call via asyncio.to_thread() or at startup.
    Returns True on success.
    """
    global _corp_code_map, _cache_loaded

    if not CORP_CODE_CACHE.exists():
        log.info("Corp code cache missing — downloading now.")
        if not _download_corp_codes():
            log.error("Corp code cache unavailable.")
            return False

    try:
        tree = ET.parse(CORP_CODE_CACHE)
        _corp_code_map.clear()
        for item in tree.getroot().findall("list"):
            code = (item.findtext("corp_code") or "").strip()
            name = (item.findtext("corp_name") or "").strip()
            if code and name:
                _corp_code_map[_normalise(name)] = code
        _cache_loaded = True
        log.info("Loaded %d corp codes from cache.", len(_corp_code_map))
        return True
    except Exception as e:
        log.error("Failed to parse corp code cache: %s", e)
        return False


def warm_up_corp_codes() -> None:
    """
    Synchronous warm-up function. Call this once at startup (inside
    asyncio.to_thread) so the corp code map is populated before any
    user request arrives.

    If the cache file already exists on disk (e.g. from a previous run
    on a persistent volume) it is parsed immediately without any network
    call. Only if it is missing does a download happen.
    """
    if _cache_loaded:
        log.info("Corp code cache already loaded — skipping warm-up.")
        return
    log.info("Corp code warm-up starting…")
    _load_corp_codes()
    log.info("Corp code warm-up complete.")


# ── Corp code lookup ───────────────────────────────────────────────────────────

def get_corp_code(company_key: str) -> str:
    """
    Return DART corp_code for company_key.
    Priority:
      1. CORP_CODE_OVERRIDES (hardcoded, instant)
      2. Name search in cached XML (via CORP_NAME_VARIANTS)
    Returns "" if not found.
    """
    key = company_key.lower()

    override = CORP_CODE_OVERRIDES.get(key, "")
    if override:
        log.debug("Corp code for %s from override: %s", key, override)
        return override

    if not _cache_loaded:
        # Lazy fallback in case warm-up was somehow skipped
        _load_corp_codes()

    if not _corp_code_map:
        log.error("Corp code map is empty — cannot look up %s.", key)
        return ""

    variants = CORP_NAME_VARIANTS.get(key, [_normalise(key)])
    for variant in variants:
        normalised_variant = _normalise(variant)
        for corp_name_norm, code in _corp_code_map.items():
            if normalised_variant in corp_name_norm:
                log.info("Corp code for %s found via variant '%s': %s", key, variant, code)
                return code

    log.warning(
        "Corp code not found for '%s'. Tried variants: %s. "
        "Set CORP_CODE_OVERRIDES['%s'] in dart.py to fix this.",
        key, variants, key,
    )
    return ""


# ── DART API fetch ─────────────────────────────────────────────────────────────

def _fmt_date(dt_str: str) -> str:
    try:
        return datetime.strptime(dt_str, "%Y%m%d").strftime("%Y-%m-%d")
    except Exception:
        return dt_str


def _get_disclosures_sync(company_key: str, limit: int = 5) -> list[dict]:
    """Synchronous DART fetch — always run via asyncio.to_thread()."""
    corp_code = get_corp_code(company_key)
    if not corp_code:
        msg = (
            f"DART corp code not found for '{company_key}'. "
            "Contact admin to configure CORP_CODE_OVERRIDES in dart.py."
        )
        log.error(msg)
        return [{"error": msg}]

    today  = datetime.now(SEOUL)
    bgn    = (today - timedelta(days=365)).strftime("%Y%m%d")
    end    = today.strftime("%Y%m%d")
    url    = "https://opendart.fss.or.kr/api/list.json"
    params = {
        "crtfc_key":  DART_API_KEY,
        "corp_code":  corp_code,
        "bgn_de":     bgn,
        "end_de":     end,
        "page_count": limit,
        "sort":       "date",
        "sort_mth":   "desc",
    }
    try:
        with httpx.Client(timeout=15) as client:
            r = client.get(url, params=params)
        r.raise_for_status()
        rows = r.json().get("list", [])[:limit]
        results = []
        for it in rows:
            rcept_no = it.get("rcept_no", "")
            results.append({
                "date":     _fmt_date(it.get("rcept_dt", "")),
                "title":    it.get("report_nm", ""),
                "rcept_no": rcept_no,
                "url":      f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}",
                "corp":     it.get("corp_name", ""),
                "pub_date": it.get("rcept_dt", ""),
            })
        log.info("DART [%s]: fetched %d disclosures (corp_code=%s).", company_key, len(results), corp_code)
        return results
    except httpx.TimeoutException:
        msg = f"DART API timeout for {company_key}"
        log.error(msg)
        return [{"error": msg}]
    except Exception as e:
        msg = f"DART API error for {company_key}: {e}"
        log.error(msg)
        return [{"error": msg}]


async def get_disclosures(company_key: str, limit: int = 5) -> list[dict]:
    """Async entry point — never blocks the event loop."""
    return await asyncio.to_thread(_get_disclosures_sync, company_key, limit)
