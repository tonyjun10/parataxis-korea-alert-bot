"""
main.py — Parataxis Korea Alert Bot entry point.

FIX: Replaced APScheduler + lambda pattern with PTB's native JobQueue.
The old pattern (lambda: app.create_task(run_monitor(app.bot))) was
unreliable on Railway because the lambda was called synchronously
by APScheduler before the Application event loop was fully running.

PTB's JobQueue runs coroutines correctly inside the running event loop.
"""

import logging
import os
import sys
from pathlib import Path

# ── Logging — stdout first so Railway captures everything ─────────────────────
Path("logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("logs/bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── Imports (after logging config) ────────────────────────────────────────────
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

import db
from handlers import (
    callback_handler,
    cmd_announcement,
    cmd_subs,
    cmd_kakao,
    cmd_t,
    cmd_kakaoexport,
    cmd_audit,
    cmd_brief,
    cmd_daily,
    cmd_mining,
    cmd_help,
    cmd_start,
    cmd_status,
    cmd_unwatch,
    cmd_users,
    cmd_watch,
    message_handler,
)
from scheduler import register_jobs
from dart import warm_up_corp_codes


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        log.critical("TELEGRAM_BOT_TOKEN is not set. Exiting.")
        sys.exit(1)

    dart_key = os.environ.get("DART_API_KEY", "")
    if not dart_key:
        log.warning("DART_API_KEY not set — DART disclosures will fail.")

    db.init_db()
    log.info("Database ready.")

    # Pre-load DART corp code cache in a background thread so the first
    # disclosure request is instant rather than waiting 10–15 minutes.
    # Using threading.Thread directly because the asyncio event loop is
    # not running yet at this point in main().
    import threading
    threading.Thread(target=warm_up_corp_codes, daemon=True, name="dart-warmup").start()
    log.info("DART corp code warm-up started in background.")

    # Build application — JobQueue is enabled by default in PTB v20+
    app = Application.builder().token(token).build()

    # ── Command handlers ───────────────────────────────────────────────────────
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("watch",   cmd_watch))
    app.add_handler(CommandHandler("unwatch", cmd_unwatch))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("audit",   cmd_audit))
    app.add_handler(CommandHandler("users",   cmd_users))
    app.add_handler(CommandHandler("brief",   cmd_brief))
    app.add_handler(CommandHandler("mining",  cmd_mining))
    app.add_handler(CommandHandler("daily",   cmd_daily))
    app.add_handler(CommandHandler("announcement", cmd_announcement))
    app.add_handler(CommandHandler("subs",         cmd_subs))
    app.add_handler(CommandHandler("kakao",        cmd_kakao))
    app.add_handler(CommandHandler("kakaoexport",  cmd_kakaoexport))
    app.add_handler(CommandHandler("t",            cmd_t))

    # ── Callback (inline buttons) ──────────────────────────────────────────────
    app.add_handler(CallbackQueryHandler(callback_handler))

    # ── Free text / search ─────────────────────────────────────────────────────
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    # ── Scheduler via PTB JobQueue (reliable on Railway) ──────────────────────
    register_jobs(app, interval_minutes=10)

    log.info("Bot starting — polling mode.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
