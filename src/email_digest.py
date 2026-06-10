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
    "tony.jun@parataxis.co.kr",
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
                st_news: list, bm_news: list, bp_news: list,
                mkt_news: list) -> str:

    parataxis_block  = _section_block("Parataxis News", [
        ("Parataxis Korea",    pk_news),
        ("Parataxis Ethereum", pe_news),
    ])
    competitor_block = _section_block("Competitor News", [
        ("Strategy",  st_news),
        ("Bitmax",    bm_news),
        ("Bitplanet", bp_news),
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
          <img src="data:image/png;base64,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"
               alt="PARATAXIS KOREA" width="220"
               style="display:block;margin-left:auto;" />
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
    mkt_news = _fetch_sheet_news(sheets_client, watchlist_sheet_id, "Market News",        max_rows=15)

    html_body = _build_html(date_str, pk_news, pe_news, st_news, bm_news, bp_news, mkt_news)
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
