"""
dart.py — DART OpenAPI integration
"""

import asyncio
import io
import logging
import os
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

# Manual overrides — fill in 8-digit DART corp_code if known
CORP_CODE_OVERRIDES: dict[str, str] = {
    "parataxis": "01227039",
    "bitmax":    "",
    "bitplanet": "",
}

_corp_code_map: dict[str, str] = {}  # name_lower -> corp_code


# ── Corp code cache ────────────────────────────────────────────────────────────

def _download_corp_codes():
    url = "https://opendart.fss.or.kr/api/corpCode.xml"
    try:
        CORP_CODE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        with httpx.Client(timeout=30) as client:
            r = client.get(url, params={"crtfc_key": DART_API_KEY})
        r.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(r.content)) as z:
            CORP_CODE_CACHE.write_bytes(z.read(z.namelist()[0]))
        log.info("Corp codes downloaded.")
    except Exception as e:
        log.error("Failed to download corp codes: %s", e)


def _load_corp_codes():
    global _corp_code_map
    if not CORP_CODE_CACHE.exists():
        _download_corp_codes()
    if not CORP_CODE_CACHE.exists():
        log.warning("Corp code cache missing — DART queries may fail.")
        return
    tree = ET.parse(CORP_CODE_CACHE)
    for item in tree.getroot().findall("list"):
        code = (item.findtext("corp_code") or "").strip()
        name = (item.findtext("corp_name") or "").strip()
        if code and name:
            _corp_code_map[name.lower()] = code
    log.info("Loaded %d corp codes.", len(_corp_code_map))


def warm_up_corp_codes():
    """Call once at startup (in a background thread) to pre-load the cache."""
    if _corp_code_map:
        return
    log.info("Corp code warm-up starting...")
    _load_corp_codes()
    log.info("Corp code warm-up complete.")


# ── Corp code lookup ───────────────────────────────────────────────────────────

def get_corp_code(company_key: str) -> str:
    override = CORP_CODE_OVERRIDES.get(company_key.lower(), "")
    if override:
        return override
    if not _corp_code_map:
        _load_corp_codes()
    keywords = {
        "parataxis": ["파라택시스", "parataxis"],
        "bitmax":    ["비트맥스", "bitmax"],
        "bitplanet": ["비트플래닛", "bitplanet"],
    }
    for kw in keywords.get(company_key.lower(), [company_key.lower()]):
        for name, code in _corp_code_map.items():
            if kw.lower() in name:
                return code
    log.warning("Corp code not found for: %s", company_key)
    return ""


# ── Disclosure fetch ───────────────────────────────────────────────────────────

def _fmt_date(dt_str: str) -> str:
    try:
        return datetime.strptime(dt_str, "%Y%m%d").strftime("%Y-%m-%d")
    except Exception:
        return dt_str


def _get_disclosures_sync(company_key: str, limit: int = 5) -> list[dict]:
    corp_code = get_corp_code(company_key)
    if not corp_code:
        return [{"error": f"Corp code not found for {company_key}."}]

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
        items = r.json().get("list", [])[:limit]
        results = []
        for it in items:
            rcept_no = it.get("rcept_no", "")
            results.append({
                "date":     _fmt_date(it.get("rcept_dt", "")),
                "title":    it.get("report_nm", ""),
                "rcept_no": rcept_no,
                "url":      f"https://dart.fss.or.kr/dsaf001/main.do?rcpNo={rcept_no}",
                "corp":     it.get("corp_name", ""),
                "pub_date": it.get("rcept_dt", ""),
            })
        return results
    except Exception as e:
        log.error("DART API error: %s", e)
        return [{"error": str(e)}]


async def get_disclosures(company_key: str, limit: int = 5) -> list[dict]:
    """Async entry point — never blocks the event loop."""
    return await asyncio.to_thread(_get_disclosures_sync, company_key, limit)

