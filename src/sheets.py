"""
sheets.py — Google Sheets integration for Kakao log.

Appends entries to 'Parataxis Kakao Log' sheet.
All failures are caught and logged — never raises to the caller.
"""

import asyncio
import logging
import os

log = logging.getLogger(__name__)

SPREADSHEET_ID  = "1oQcNwpGjePKFvUaIyN44tKU04RtQpHBCZNMy1q2scbg"
SHEET_NAME      = "Parataxis Kakao Log"
SHEET_URL       = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}"
_KEY_FILE       = os.path.join(os.path.dirname(__file__), "parataxis-kakao-log-e0ffbd9adab6.json")
_SCOPES         = ["https://www.googleapis.com/auth/spreadsheets"]


def _append_sync(timestamp: str, user: str, message: str) -> None:
    """Blocking append — run via asyncio.to_thread."""
    import gspread
    from google.oauth2.service_account import Credentials

    creds  = Credentials.from_service_account_file(_KEY_FILE, scopes=_SCOPES)
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
