"""
brief.py — Headless screenshot using system Chromium.

Uses the system Chromium installed via apt (/usr/bin/chromium) instead of
Playwright's downloaded browser. This bypasses all the PLAYWRIGHT_BROWSERS_PATH
env var issues — the binary is always at a fixed, known path.
"""

import asyncio
import logging
import shutil

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

log = logging.getLogger(__name__)

DASHBOARD_EN         = "https://btc-tracker.up.railway.app/global-tracker"
DASHBOARD_KO         = "https://btc-tracker.up.railway.app/korea-tracker"
SCREENSHOT_TIMEOUT_S = 60
PAGE_TIMEOUT_MS      = 30_000
DATA_TIMEOUT_MS      = 25_000
SETTLE_MS            = 1_000
VIEWPORT_W           = 1_400
VIEWPORT_H           = 900

# System Chromium path (installed via apt in Dockerfile)
CHROMIUM_PATH = "/usr/bin/chromium"


class BriefError(Exception):
    pass


def _url_for_lang(lang: str) -> str:
    return DASHBOARD_KO if lang == "ko" else DASHBOARD_EN


async def take_screenshot(lang: str) -> bytes:
    url          = _url_for_lang(lang)
    loading_text = "연결 중..." if lang == "ko" else "Connecting..."
    log.info("[brief] screenshotting %s", url)

    # Verify the binary exists before trying to launch
    if not shutil.which("chromium") and not __import__("os").path.exists(CHROMIUM_PATH):
        raise BriefError(f"Chromium not found at {CHROMIUM_PATH}")

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                executable_path=CHROMIUM_PATH,
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
                await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)

                # Wait until #update-time changes from loading placeholder to real timestamp
                await page.wait_for_function(
                    f"document.getElementById('update-time') && "
                    f"document.getElementById('update-time').textContent !== '{loading_text}' && "
                    f"document.getElementById('update-time').textContent !== ''",
                    timeout=DATA_TIMEOUT_MS,
                )

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
