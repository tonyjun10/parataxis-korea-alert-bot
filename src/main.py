"""
main.py — Parataxis Korea Alert Bot entry point.
"""

import logging
import os
import sys
from pathlib import Path

# ── Logging ────────────────────────────────────────────────────────────────────
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

# ── Imports (after logging) ────────────────────────────────────────────────────
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler

import db
from handlers import (
    cmd_start, cmd_help, cmd_watch, cmd_unwatch, cmd_status,
    cmd_audit, cmd_users,
    callback_handler, message_handler,
)
from scheduler import run_monitor


def main():
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        log.critical("TELEGRAM_BOT_TOKEN is not set. Exiting.")
        sys.exit(1)

    dart_key = os.environ.get("DART_API_KEY", "")
    if not dart_key:
        log.warning("DART_API_KEY is not set — DART disclosures will fail.")

    db.init_db()

    app = Application.builder().token(token).build()

    # Public commands
    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("help",    cmd_help))
    app.add_handler(CommandHandler("watch",   cmd_watch))
    app.add_handler(CommandHandler("unwatch", cmd_unwatch))
    app.add_handler(CommandHandler("status",  cmd_status))

    # Admin-only commands (permission checked inside handler)
    app.add_handler(CommandHandler("audit",   cmd_audit))
    app.add_handler(CommandHandler("users",   cmd_users))

    # Callbacks (buttons)
    app.add_handler(CallbackQueryHandler(callback_handler))

    # Free text / search
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    # ── Scheduler ──────────────────────────────────────────────────────────────
    scheduler = AsyncIOScheduler(timezone="Asia/Seoul")
    scheduler.add_job(
        lambda: app.create_task(run_monitor(app.bot)),
        trigger="interval",
        minutes=10,
        id="monitor",
    )
    scheduler.start()
    log.info("Scheduler started — monitor every 10 min.")

    log.info("Bot starting in polling mode…")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()