"""
sheets.py — Google Sheets integration for Kakao log.

Credentials loaded from GOOGLE_SHEETS_CREDENTIALS env var (base64-encoded JSON).
All failures are caught and logged — never raises to the caller.
"""

import asyncio
import base64
import json
import logging
import os

log = logging.getLogger(__name__)

SPREADSHEET_ID        = "1oQcNwpGjePKFvUaIyN44tKU04RtQpHBCZNMy1q2scbg"
WATCHLIST_SHEET_ID    = "1xYq-GoAHIvybe_GyUZtJyWp-Dy83Zl38A6anKxc9uWk"
WATCHLIST_SHEET_URL   = f"https://docs.google.com/spreadsheets/d/{WATCHLIST_SHEET_ID}"

# Maps internal company key → sheet tab name
_WATCHLIST_TABS = {
    "parataxis":     "Parataxis",
    "bitmax":        "Bitmax",
    "bitplanet":     "Bitplanet",
    "parataxiseth":  "Parataxis Ethereum",
    "microstrategy": "Strategy",
    "market_news":   "Market News",
}
SHEET_NAME     = "Parataxis Kakao Log"
SHEET_URL      = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"
_SCOPES        = ["https://www.googleapis.com/auth/spreadsheets"]


def _append_sync(timestamp: str, user: str, message: str) -> None:
    """Blocking append — run via asyncio.to_thread."""
    import gspread
    from google.oauth2.service_account import Credentials

    raw = os.environ.get("GOOGLE_SHEETS_CREDENTIALS", "")
    if not raw:
        raise RuntimeError("GOOGLE_SHEETS_CREDENTIALS env var not set")

    # Decode base64 — avoids Railway mangling newlines in the private key
    info = json.loads(base64.b64decode(raw.strip()).decode("utf-8"))

    creds  = Credentials.from_service_account_info(info, scopes=_SCOPES)
    client = gspread.authorize(creds)
    sheet  = client.open_by_key(SPREADSHEET_ID).worksheet(SHEET_NAME)
    sheet.append_row(
        [timestamp, user, message],
        value_input_option="RAW",
        insert_data_option="INSERT_ROWS",
    )


async def append_kakao_entry(timestamp: str, user: str, message: str) -> None:
    """
    Async wrapper. Silently logs any error — never raises.
    Safe to fire-and-forget from cmd_kakao.
    """
    try:
        await asyncio.to_thread(_append_sync, timestamp, user, message)
        log.info("[sheets] kakao entry appended for %s", user)
    except Exception as exc:
        log.warning("[sheets] append failed (non-fatal): %s", exc)


# ── Watchlist logging (news + disclosures) ─────────────────────────────────────

# Cached gspread client — avoids re-authenticating on every call
_gspread_client = None

def _get_gspread_client():
    """Return a cached gspread client, creating one if needed."""
    global _gspread_client
    import gspread
    from google.oauth2.service_account import Credentials
    if _gspread_client is None:
        raw = os.environ.get("GOOGLE_SHEETS_CREDENTIALS", "")
        if not raw:
            raise RuntimeError("GOOGLE_SHEETS_CREDENTIALS env var not set")
        info          = json.loads(base64.b64decode(raw.strip()).decode("utf-8"))
        creds         = Credentials.from_service_account_info(info, scopes=_SCOPES)
        _gspread_client = gspread.authorize(creds)
    return _gspread_client


def _append_watchlist_sync(company: str, entry_type: str, title: str, url: str) -> None:
    """Blocking append to the watchlist sheet. Run via asyncio.to_thread."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tab = _WATCHLIST_TABS.get(company)
    if not tab:
        raise ValueError(f"No watchlist tab for company: {company}")

    try:
        client = _get_gspread_client()
        sheet  = client.open_by_key(WATCHLIST_SHEET_ID).worksheet(tab)
    except Exception:
        # If cached client fails, reset and retry once with a fresh one
        global _gspread_client
        _gspread_client = None
        client = _get_gspread_client()
        sheet  = client.open_by_key(WATCHLIST_SHEET_ID).worksheet(tab)

    ts = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M KST")
    sheet.append_row(
        [ts, entry_type, title, url],
        value_input_option="RAW",
        insert_data_option="INSERT_ROWS",
    )


async def append_watchlist_entry(company: str, entry_type: str, title: str, url: str) -> None:
    """
    Async wrapper. Silently logs any error — never raises.
    entry_type should be 'News' or 'Disclosure'.
    """
    try:
        await asyncio.to_thread(_append_watchlist_sync, company, entry_type, title, url)
        log.info("[sheets] watchlist entry appended: %s / %s", company, entry_type)
    except Exception as exc:
        log.warning("[sheets] watchlist append failed (non-fatal): %s", exc)
