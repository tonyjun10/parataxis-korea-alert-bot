"""
brief.py — Headless screenshot of the BTC dashboard.

The dashboard pages (bitcoin-tracker.html / bitcoin-tracker-ko.html) fetch
data from /api/data and /api/korea-data on load. Those calls hit external
APIs (Coinpaprika, Upbit, Bithumb) which can take several seconds. The page
also runs setInterval(fetchData, 60000) forever, so Playwright's networkidle
strategy never fires — the page is always doing network activity.

Fix: use wait_for_selector on #update-time whose text changes from
"Connecting..." / "연결 중..." to "Updated: ..." / "업데이트: ..." only
after the first successful data fetch. This is the exact moment the page
is fully populated and ready to screenshot.
"""

import asyncio
import logging

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

log = logging.getLogger(__name__)

DASHBOARD_EN         = "https://btc-tracker.up.railway.app/global-tracker"
DASHBOARD_KO         = "https://btc-tracker.up.railway.app/korea-tracker"
SCREENSHOT_TIMEOUT_S = 60       # hard budget for the whole operation
PAGE_TIMEOUT_MS      = 30_000   # page.goto timeout
DATA_TIMEOUT_MS      = 25_000   # how long to wait for #update-time to change
SETTLE_MS            = 1_000    # brief pause after data loads for chart paint
VIEWPORT_W           = 1_400
VIEWPORT_H           = 900


class BriefError(Exception):
    pass


def _url_for_lang(lang: str) -> str:
    return DASHBOARD_KO if lang == "ko" else DASHBOARD_EN


async def take_screenshot(lang: str) -> bytes:
    url = _url_for_lang(lang)
    log.info("[brief] screenshotting %s (lang=%s)", url, lang)

    # The text that #update-time shows BEFORE data loads
    loading_text = "연결 중..." if lang == "ko" else "Connecting..."

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            try:
                page = await browser.new_page(
                    viewport={"width": VIEWPORT_W, "height": VIEWPORT_H},
                )

                # Load the page — domcontentloaded is enough, we'll wait for
                # data ourselves via wait_for_selector below
                await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)

                # Wait until #update-time no longer shows the loading placeholder.
                # This fires the instant the first API call completes and
                # updateMetrics() writes the real timestamp into the element.
                await page.wait_for_function(
                    f"document.getElementById('update-time') && "
                    f"document.getElementById('update-time').textContent !== '{loading_text}' && "
                    f"document.getElementById('update-time').textContent !== ''",
                    timeout=DATA_TIMEOUT_MS,
                )

                # Brief settle for Chart.js canvas to finish painting
                await asyncio.sleep(SETTLE_MS / 1_000)

                png_bytes = await page.screenshot(full_page=True)
                log.info("[brief] screenshot taken — %d bytes", len(png_bytes))
                return png_bytes

            finally:
                await browser.close()

    except PWTimeout as exc:
        raise BriefError(f"Timed out waiting for dashboard data ({url})") from exc
    except Exception as exc:
        raise BriefError(f"Screenshot failed for {url}: {exc}") from exc


async def take_screenshot_with_timeout(lang: str) -> bytes:
    try:
        return await asyncio.wait_for(
            take_screenshot(lang),
            timeout=SCREENSHOT_TIMEOUT_S,
        )
    except asyncio.TimeoutError as exc:
        raise BriefError(f"Screenshot timed out after {SCREENSHOT_TIMEOUT_S}s") from exc
