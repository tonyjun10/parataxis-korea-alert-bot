"""
email_digest.py — Daily Parataxis News Digest via SendGrid.

Pulls latest news from the Google Sheets watchlist tabs and sends a
formatted HTML email to the recipient list.

Required env vars:
  SENDGRID_API_KEY  — SendGrid API key
  EMAIL_SENDER      — sender address (e.g. tony.jun@parataxis.co.kr)

Optional env vars:
  EMAIL_TEST_MODE   — if set to "1", sends only to EMAIL_SENDER (testing)
"""

import logging
import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx

log = logging.getLogger(__name__)
SEOUL = ZoneInfo("Asia/Seoul")

# ── Recipients ────────────────────────────────────────────────────────────────
# Add exec emails here when ready to go live (EMAIL_TEST_MODE=0)
ALL_RECIPIENTS = [
    "lunakim13@gmail.com",
    "balloonpoppingboy@gmail.com",
    "tonyjun1010@gmail.com",
]

# ── Fetch news from sheets ────────────────────────────────────────────────────

def _fetch_sheet_news(sheets_client, spreadsheet_id: str, tab_name: str, max_rows: int = 50) -> list[dict]:
    """
    Fetch approved news rows from a Google Sheet tab.
    Only returns rows where column E (Send) = 'YES'.
    After collecting, clears the YES flags so articles aren't re-sent tomorrow.
    """
    try:
        sheet = sheets_client.open_by_key(spreadsheet_id).worksheet(tab_name)
        rows  = sheet.get_all_values()
        if len(rows) <= 1:
            return []

        results = []
        rows_to_clear = []  # (row_index, ) for clearing YES after send

        for i, row in enumerate(rows[1:], start=2):  # start=2 because row 1 is header
            # Column E is index 4
            approved = row[4].strip().upper() if len(row) >= 5 else ""
            if approved != "YES":
                continue
            if len(row) >= 4 and row[2].strip():
                results.append({
                    "timestamp": row[0],
                    "type":      row[1],
                    "title":     row[2],
                    "url":       row[3],
                })
                rows_to_clear.append(i)

        # Clear YES flags after collecting so articles aren't re-sent
        for row_idx in rows_to_clear:
            sheet.update_cell(row_idx, 5, "")  # Column 5 = E

        log.info("[email] Tab '%s': %d approved articles, cleared flags.", tab_name, len(results))
        return results

    except Exception as e:
        log.warning("[email] Failed to fetch tab '%s': %s", tab_name, e)
        return []


# ── HTML template ─────────────────────────────────────────────────────────────

def _articles_html(items: list[dict]) -> str:
    if not items:
        return '<tr><td style="padding:4px 0 4px 24px;color:#999;font-size:12px;">No recent articles found.</td></tr>'
    html = ""
    for item in items:
        title = item.get("title", "").replace("<", "&lt;").replace(">", "&gt;")
        url   = item.get("url", "#")
        html += f'''
      <tr><td style="padding:4px 0 4px 24px;border-bottom:1px solid #f0f0f0;">
        <a href="{url}" style="font-size:13px;color:#1a1a2e;text-decoration:none;">{title}</a>
      </td></tr>'''
    return html


def _section_block(section_title: str, subsections: list[tuple[str, list[dict]]]) -> str:
    subsection_html = ""
    for sub_title, items in subsections:
        subsection_html += f'''
      <tr><td style="padding:8px 0 4px 12px;">
        <span style="font-size:13px;font-weight:600;color:#444;">::  {sub_title}  ::</span>
      </td></tr>
      {_articles_html(items)}'''

    return f'''
      <tr><td style="padding:20px 0 8px 0;">
        <table width="100%" cellpadding="0" cellspacing="0">
          <tr><td style="border-left:4px solid #1a1a2e;padding-left:10px;">
            <span style="font-size:15px;font-weight:700;color:#1a1a2e;
                  text-transform:uppercase;letter-spacing:1px;">■ {section_title}</span>
          </td></tr>
        </table>
      </td></tr>
      {subsection_html}'''


def _build_html(date_str: str,
                pk_news: list, pe_news: list,
                st_news: list, bm_news: list, bp_news: list, bn_news: list,
                mkt_news: list) -> str:

    parataxis_block  = _section_block("Parataxis News", [
        ("Parataxis Korea",    pk_news),
        ("Parataxis Ethereum", pe_news),
    ])
    competitor_block = _section_block("Competitor News", [
        ("Strategy",  st_news),
        ("Bitmax",    bm_news),
        ("Bitplanet", bp_news),
        ("Bitmine",   bn_news),
    ])
    market_block     = _section_block("Market News", [
        ("Market News", mkt_news),
    ])

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"></head>
<body style="margin:0;padding:20px;background:#f5f5f5;font-family:Arial,sans-serif;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;">
<tr><td align="center">
<table width="680" cellpadding="0" cellspacing="0"
       style="background:#ffffff;border:1px solid #e0e0e0;border-radius:4px;">

  <!-- Header -->
  <tr><td style="background:#ffffff;padding:24px 32px;border-bottom:2px solid #1a1a2e;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td>
          <div style="color:#1a1a2e;font-size:11px;letter-spacing:2px;
                      text-transform:uppercase;margin-bottom:6px;">Daily Monitoring</div>
          <div style="color:#555;font-size:11px;margin-bottom:2px;">Built on trust.</div>
          <div style="color:#555;font-size:11px;margin-bottom:2px;">Powered by expertise.</div>
          <div style="color:#555;font-size:11px;">Led by operators.</div>
        </td>
        <td align="right" valign="middle">
          <img src="https://btc-tracker.up.railway.app/parataxis_logo.png" alt="PARATAXIS" width="220" style="display:block;margin-left:auto;" />
          <div style="color:#555;font-size:11px;text-align:right;margin-top:6px;">{date_str}</div>
        </td>
      </tr>
    </table>
  </td></tr>

  <!-- Body -->
  <tr><td style="padding:24px 32px;">
    <table width="100%" cellpadding="0" cellspacing="0">
      {parataxis_block}
      {competitor_block}
      {market_block}
    </table>
  </td></tr>

  <!-- Footer -->
  <tr><td style="padding:16px 32px;border-top:1px solid #e0e0e0;
                  background:#fafafa;text-align:center;">
    <span style="font-size:11px;color:#999;">
      ※ 본 내용은 모든 뉴스의 저작권 및 지적재산권은 해당 매체와 정보 제공처에 있음을 알려드립니다
    </span><br>
    <span style="font-size:11px;color:#999;">
      Parataxis Korea · Daily News Digest · Automated Report
    </span>
  </td></tr>

</table>
</td></tr>
</table>
</body>
</html>"""


# ── Send via SendGrid ─────────────────────────────────────────────────────────

def send_digest(sheets_client, watchlist_sheet_id: str) -> bool:
    """Build and send the daily digest. Returns True on success."""
    api_key   = os.environ.get("SENDGRID_API_KEY", "")
    sender    = os.environ.get("EMAIL_SENDER", "tony.jun@parataxis.co.kr")
    test_mode = os.environ.get("EMAIL_TEST_MODE", "1") == "1"

    if not api_key:
        log.warning("[email] SENDGRID_API_KEY not set — skipping")
        return False

    now      = datetime.now(SEOUL)
    date_str = now.strftime("%Y. %-m. %-d")

    # Fetch from each sheet tab
    pk_news  = _fetch_sheet_news(sheets_client, watchlist_sheet_id, "Parataxis",          max_rows=8)
    pe_news  = _fetch_sheet_news(sheets_client, watchlist_sheet_id, "Parataxis Ethereum", max_rows=8)
    st_news  = _fetch_sheet_news(sheets_client, watchlist_sheet_id, "Strategy",           max_rows=5)
    bm_news  = _fetch_sheet_news(sheets_client, watchlist_sheet_id, "Bitmax",             max_rows=5)
    bp_news  = _fetch_sheet_news(sheets_client, watchlist_sheet_id, "Bitplanet",          max_rows=5)
    bn_news  = _fetch_sheet_news(sheets_client, watchlist_sheet_id, "Bitmine",            max_rows=5)
    mkt_news = _fetch_sheet_news(sheets_client, watchlist_sheet_id, "Market News",        max_rows=15)

    html_body = _build_html(date_str, pk_news, pe_news, st_news, bm_news, bp_news, bn_news, mkt_news)
    subject   = f"[Parataxis] Daily News Report — {now.strftime('%B %-d, %Y')}"

    # Test mode: send only to sender
    recipients = [sender] if test_mode else ALL_RECIPIENTS
    log.info("[email] Sending digest to %d recipient(s) (test_mode=%s)", len(recipients), test_mode)

    payload = {
        "personalizations": [{"to": [{"email": r} for r in recipients]}],
        "from":    {"email": sender, "name": "Parataxis Daily Digest"},
        "subject": subject,
        "content": [{"type": "text/html", "value": html_body}],
    }

    try:
        r = httpx.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
            },
            content=json.dumps(payload),
            timeout=30,
        )
        if r.status_code == 202:
            log.info("[email] Digest sent successfully.")
            return True
        else:
            log.error("[email] SendGrid error: %d %s", r.status_code, r.text[:300])
            return False
    except Exception as e:
        log.error("[email] Send exception: %s", e)
        return False
