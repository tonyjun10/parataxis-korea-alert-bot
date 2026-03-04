"""
brief.py — Headless screenshot of the BTC dashboard.

Design decisions:
  - Uses Playwright (async API) so it runs natively in the asyncio event loop
    without needing asyncio.to_thread(). Playwright's own async context manager
    handles the subprocess lifecycle.
  - Two URLs, chosen by language:
      en → DASHBOARD_EN  (/global-tracker)
      ko → DASHBOARD_KO  (/korea-tracker)
  - Waits for networkidle (all XHR/fetch settled) then an extra SETTLE_MS to
    let JS-rendered charts finish painting before the shutter fires.
  - Returns raw PNG bytes on success; raises BriefError on any failure so
    callers can show a clean message.
  - SCREENSHOT_TIMEOUT_S covers the full browser + load + screenshot cycle.
    If Railway is slow to start Chromium, this gives it plenty of headroom.
"""

import asyncio
import logging

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

log = logging.getLogger(__name__)

# ── Configuration ──────────────────────────────────────────────────────────────
DASHBOARD_EN       = "https://btc-tracker.up.railway.app/global-tracker"
DASHBOARD_KO       = "https://btc-tracker.up.railway.app/korea-tracker"
SCREENSHOT_TIMEOUT_S = 45        # whole operation budget (seconds)
PAGE_TIMEOUT_MS      = 30_000    # Playwright page.goto timeout (ms)
SETTLE_MS            = 2_000     # extra wait after networkidle for JS paint (ms)
VIEWPORT_W           = 1_400     # px — wide enough for dashboard layout
VIEWPORT_H           = 900       # px


class BriefError(Exception):
    """Raised when a screenshot cannot be taken."""


def _url_for_lang(lang: str) -> str:
    return DASHBOARD_KO if lang == "ko" else DASHBOARD_EN


async def take_screenshot(lang: str) -> bytes:
    """
    Launch a headless Chromium instance, load the appropriate dashboard page,
    wait for it to fully render, and return a PNG screenshot as bytes.

    Raises BriefError on any failure (timeout, navigation error, etc.).
    """
    url = _url_for_lang(lang)
    log.info("[brief] screenshotting %s (lang=%s)", url, lang)

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",   # avoids /dev/shm OOM on Railway
                    "--disable-gpu",
                ],
            )
            try:
                page = await browser.new_page(
                    viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
                )
                # Navigate and wait until all network activity has settled
                await page.goto(url, wait_until="networkidle", timeout=PAGE_TIMEOUT_MS)
                # Extra settle time for JS-rendered charts / canvas elements
                await asyncio.sleep(SETTLE_MS / 1_000)
                # Full-page screenshot returns the entire scrollable content
                png_bytes = await page.screenshot(full_page=True)
                log.info("[brief] screenshot taken — %d bytes", len(png_bytes))
                return png_bytes
            finally:
                await browser.close()

    except PWTimeout as exc:
        msg = f"Page load timed out for {url}: {exc}"
        log.error("[brief] %s", msg)
        raise BriefError(msg) from exc
    except Exception as exc:
        msg = f"Screenshot failed for {url}: {exc}"
        log.error("[brief] %s", msg, exc_info=True)
        raise BriefError(msg) from exc


async def take_screenshot_with_timeout(lang: str) -> bytes:
    """
    Wraps take_screenshot() with a hard overall timeout (SCREENSHOT_TIMEOUT_S).
    Raises BriefError if the entire operation exceeds the budget.
    """
    try:
        return await asyncio.wait_for(
            take_screenshot(lang),
            timeout=SCREENSHOT_TIMEOUT_S,
        )
    except asyncio.TimeoutError as exc:
        msg = f"Screenshot timed out after {SCREENSHOT_TIMEOUT_S}s"
        log.error("[brief] %s", msg)
        raise BriefError(msg) from exc
