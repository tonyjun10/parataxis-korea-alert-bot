"""
dart.py — DART OpenAPI integration.

Fixes applied:
  - CORP_CODE_OVERRIDES now has a real entry point for "parataxis" (fill in
    the actual corp_code once known; leave empty to fall back to name search).
  - Lookup tries multiple Korean AND English name variants, normalising
    whitespace so spacing differences never cause misses.
  - All HTTP calls have explicit timeouts.
  - Everything blocking runs in asyncio.to_thread() — bot never hangs.
  - Clear error messages returned to UI when corp code cannot be found.
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
# Find your corp_code at: https://opendart.fss.or.kr/
CORP_CODE_OVERRIDES: dict[str, str] = {
    "parataxis": "01227039",   # e.g. "00123456" — fill in once confirmed
    "bitmax":    "",
    "bitplanet": "",
}

# Name variants to search in the DART corp code XML.
# Each entry is a list of substrings; we check if ANY substring is contained
# in the normalised (whitespace-stripped, lower-case) corp name.
CORP_NAME_VARIANTS: dict[str, list[str]] = {
    "parataxis": [
        "파라택시스",
        "parataxis",
        "para taxis",   # handles unexpected spacing
    ],
    "bitmax": [
        "비트맥스",
        "bitmax",
    ],
    "bitplanet": [
        "비트플래닛",
        "bitplanet",
    ],
}

_corp_code_map: dict[str, str] = {}   # normalised_name -> corp_code
_cache_loaded:  bool            = False


def _normalise(s: str) -> str:
    """Lower-case and collapse all whitespace for comparison."""
    return re.sub(r"\s+", "", s.lower())


def _load_corp_codes() -> bool:
    """Load corp codes from cache file. Return True on success."""
    global _corp_code_map, _cache_loaded
    if not CORP_CODE_CACHE.exists():
        log.info("Corp code cache missing — downloading.")
        _download_corp_codes()
    if not CORP_CODE_CACHE.exists():
        log.error("Corp code cache still missing after download attempt.")
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


def _download_corp_codes():
    """Download and unzip the DART corp code XML. Blocking — call via to_thread."""
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
    except Exception as e:
        log.error("Corp code download failed: %s", e)


def get_corp_code(company_key: str) -> str:
    """
    Return DART corp_code for company_key.
    Priority:
      1. CORP_CODE_OVERRIDES (hardcoded, instant)
      2. Name search in cached XML (via CORP_NAME_VARIANTS)
    Returns "" if not found.
    """
    key = company_key.lower()

    # 1. Hard override
    override = CORP_CODE_OVERRIDES.get(key, "")
    if override:
        log.debug("Corp code for %s from override: %s", key, override)
        return override

    # 2. Load cache if needed
    if not _cache_loaded:
        _load_corp_codes()

    if not _corp_code_map:
        log.error("Corp code map is empty — cannot look up %s.", key)
        return ""

    # 3. Try each name variant
    variants = CORP_NAME_VARIANTS.get(key, [_normalise(key)])
    for variant in variants:
        normalised_variant = _normalise(variant)
        # Exact substring match in normalised corp names
        for corp_name_norm, code in _corp_code_map.items():
            if normalised_variant in corp_name_norm:
                log.info("Corp code for %s found via variant '%s': %s", key, variant, code)
                return code

    log.warning(
        "Corp code not found for '%s'. Tried variants: %s. "
        "Set CORP_CODE_OVERRIDES['%s'] in dart.py to fix this.",
        key, variants, key
    )
    return ""


def _fmt_date(dt_str: str) -> str:
    try:
        return datetime.strptime(dt_str, "%Y%m%d").strftime("%Y-%m-%d")
    except Exception:
        return dt_str


def _get_disclosures_sync(company_key: str, limit: int = 10) -> list[dict]:
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
        raw  = r.json()
        rows = raw.get("list", [])[:limit]
        results = []
        for it in rows:
            rcept_no = it.get("rcept_no", "")
            results.append({
                "date":     _fmt_date(it.get("rcept_dt", "")),
                "title":    it.get("report_nm", ""),
                "rcept_no": rcept_no,
                "url":      f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}",
                "corp":     it.get("corp_name", ""),
                "pub_date": it.get("rcept_dt", ""),  # raw YYYYMMDD for sorting
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


async def get_disclosures(company_key: str, limit: int = 10) -> list[dict]:
    """Async entry point — never blocks the event loop."""
    return await asyncio.to_thread(_get_disclosures_sync, company_key, limit)
