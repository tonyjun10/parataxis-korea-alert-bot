"""
dart.py — DART OpenAPI integration.
"""

import asyncio
import io
import logging
import os
import re
import threading
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

CORP_CODE_OVERRIDES: dict[str, str] = {
    "parataxis": "",   # fill in 8-digit DART corp_code once confirmed
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
_load_lock = threading.Lock()   # prevents simultaneous load attempts


def _normalise(s: str) -> str:
    return re.sub(r"\s+", "", s.lower())


# ── Cache load / download ──────────────────────────────────────────────────────

def _download_corp_codes() -> bool:
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


def _load_corp_codes_locked() -> bool:
    """
    Load corp codes under the lock. Must only be called while holding _load_lock.
    If another thread already loaded while we waited, returns True immediately.
    """
    global _corp_code_map, _cache_loaded

    # Re-check after acquiring lock — another thread may have finished while we waited
    if _cache_loaded:
        return True

    if not CORP_CODE_CACHE.exists():
        log.info("Corp code cache missing — downloading now.")
        if not _download_corp_codes():
            log.error("Corp code cache unavailable after download attempt.")
            return False

    try:
        tree = ET.parse(CORP_CODE_CACHE)
        new_map: dict[str, str] = {}
        for item in tree.getroot().findall("list"):
            code = (item.findtext("corp_code") or "").strip()
            name = (item.findtext("corp_name") or "").strip()
            if code and name:
                new_map[_normalise(name)] = code
        _corp_code_map = new_map   # atomic dict replacement
        _cache_loaded  = True
        log.info("Loaded %d corp codes.", len(_corp_code_map))
        return True
    except Exception as e:
        log.error("Failed to parse corp code cache: %s", e)
        return False


def _ensure_loaded() -> bool:
    """
    Ensure the corp code map is loaded. Thread-safe: at most one thread
    runs the actual load/download; all others wait and reuse the result.
    """
    if _cache_loaded:          # fast path — no lock needed once loaded
        return True
    with _load_lock:
        return _load_corp_codes_locked()


def warm_up_corp_codes() -> None:
    """
    Call once at startup (in a background thread from main.py).
    Blocks until the XML is downloaded and parsed, so subsequent
    calls to get_corp_code() are instant.
    """
    log.info("Corp code warm-up starting...")
    ok = _ensure_loaded()
    log.info("Corp code warm-up %s.", "complete" if ok else "FAILED")


# ── Corp code lookup ───────────────────────────────────────────────────────────

def get_corp_code(company_key: str) -> str:
    key = company_key.lower()

    override = CORP_CODE_OVERRIDES.get(key, "")
    if override:
        return override

    if not _ensure_loaded():
        log.error("Corp code map unavailable — cannot look up %s.", key)
        return ""

    variants = CORP_NAME_VARIANTS.get(key, [_normalise(key)])
    for variant in variants:
        needle = _normalise(variant)
        for corp_name_norm, code in _corp_code_map.items():
            if needle in corp_name_norm:
                log.info("Corp code for %s: %s (via '%s')", key, code, variant)
                return code

    log.warning(
        "Corp code not found for '%s'. Tried: %s. "
        "Run find_corp_code.py to locate the right code and set CORP_CODE_OVERRIDES.",
        key, variants,
    )
    return ""


# ── DART API fetch ─────────────────────────────────────────────────────────────

def _fmt_date(dt_str: str) -> str:
    try:
        return datetime.strptime(dt_str, "%Y%m%d").strftime("%Y-%m-%d")
    except Exception:
        return dt_str


def _get_disclosures_sync(company_key: str, limit: int = 5) -> list[dict]:
    corp_code = get_corp_code(company_key)
    if not corp_code:
        msg = (
            f"DART corp code not found for '{company_key}'. "
            "Run find_corp_code.py then set CORP_CODE_OVERRIDES in dart.py."
        )
        log.error(msg)
        return [{"error": msg}]

    today  = datetime.now(SEOUL)
    bgn    = (today - timedelta(days=365)).strftime("%Y%m%d")
    end    = today.strftime("%Y%m%d")
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
            r = client.get("https://opendart.fss.or.kr/api/list.json", params=params)
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
        log.info("DART [%s]: %d disclosures (corp_code=%s).", company_key, len(results), corp_code)
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
    return await asyncio.to_thread(_get_disclosures_sync, company_key, limit)
