"""
db.py — SQLite persistence layer
"""

import sqlite3
import hashlib
import logging
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

DB_PATH = Path("data/bot.db")
SEOUL   = ZoneInfo("Asia/Seoul")
log     = logging.getLogger(__name__)


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            -- Core subscription table (new schema with per-category flags)
            CREATE TABLE IF NOT EXISTS subscriptions (
                chat_id               INTEGER PRIMARY KEY,
                subscribed            INTEGER NOT NULL DEFAULT 1,
                disclosures_enabled   INTEGER NOT NULL DEFAULT 1,
                news_enabled          INTEGER NOT NULL DEFAULT 1,
                strategy_enabled      INTEGER NOT NULL DEFAULT 1,
                language              TEXT    NOT NULL DEFAULT 'en',
                created_at            TEXT    DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS user_lang (
                chat_id  INTEGER PRIMARY KEY,
                lang     TEXT NOT NULL DEFAULT 'en'
            );

            CREATE TABLE IF NOT EXISTS seen_disclosures (
                rcept_no   TEXT PRIMARY KEY,
                company    TEXT,
                seen_at    TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS seen_news (
                url_hash   TEXT PRIMARY KEY,
                company    TEXT,
                seen_at    TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS seen_strategy (
                item_hash  TEXT PRIMARY KEY,
                company    TEXT,
                seen_at    TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS audit_log (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp   TEXT NOT NULL,
                event_type  TEXT NOT NULL,
                user_id     INTEGER,
                username    TEXT,
                chat_id     INTEGER,
                payload     TEXT
            );
        """)
        _migrate(conn)
    log.info("Database initialised at %s", DB_PATH)


def _migrate(conn: sqlite3.Connection):
    """
    Add new columns to subscriptions if upgrading from old schema.
    SQLite does not support IF NOT EXISTS on ALTER TABLE, so we check first.
    """
    cols = {row[1] for row in conn.execute("PRAGMA table_info(subscriptions)")}
    migrations = [
        ("disclosures_enabled", "INTEGER NOT NULL DEFAULT 1"),
        ("news_enabled",        "INTEGER NOT NULL DEFAULT 1"),
        ("strategy_enabled",    "INTEGER NOT NULL DEFAULT 1"),
        ("language",            "TEXT    NOT NULL DEFAULT 'en'"),
    ]
    for col, definition in migrations:
        if col not in cols:
            conn.execute(f"ALTER TABLE subscriptions ADD COLUMN {col} {definition}")
            log.info("Migration: added column subscriptions.%s", col)


# ── Subscriptions ──────────────────────────────────────────────────────────────

def subscribe(chat_id: int, category: str = "all", lang: str = "en"):
    """
    Subscribe chat_id. category is one of: all / disclosures / news / strategy.
    Existing rows are updated; missing category-enables default to 1.
    """
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT chat_id FROM subscriptions WHERE chat_id=?", (chat_id,)
        ).fetchone()

        if not existing:
            conn.execute(
                "INSERT INTO subscriptions"
                "  (chat_id, subscribed, disclosures_enabled, news_enabled, strategy_enabled, language)"
                "  VALUES (?,1,1,1,1,?)",
                (chat_id, lang),
            )

        if category == "all":
            conn.execute(
                "UPDATE subscriptions SET subscribed=1,"
                "  disclosures_enabled=1, news_enabled=1, strategy_enabled=1,"
                "  language=? WHERE chat_id=?",
                (lang, chat_id),
            )
        elif category == "disclosures":
            conn.execute(
                "UPDATE subscriptions SET subscribed=1, disclosures_enabled=1,"
                "  language=? WHERE chat_id=?",
                (lang, chat_id),
            )
        elif category == "news":
            conn.execute(
                "UPDATE subscriptions SET subscribed=1, news_enabled=1,"
                "  language=? WHERE chat_id=?",
                (lang, chat_id),
            )
        elif category == "strategy":
            conn.execute(
                "UPDATE subscriptions SET subscribed=1, strategy_enabled=1,"
                "  language=? WHERE chat_id=?",
                (lang, chat_id),
            )


def unsubscribe(chat_id: int, category: str = "all"):
    """
    Unsubscribe category. If all categories become 0, set subscribed=0 too.
    """
    with get_conn() as conn:
        if category == "all":
            conn.execute(
                "UPDATE subscriptions SET subscribed=0,"
                "  disclosures_enabled=0, news_enabled=0, strategy_enabled=0"
                "  WHERE chat_id=?",
                (chat_id,),
            )
            return

        col_map = {
            "disclosures": "disclosures_enabled",
            "news":        "news_enabled",
            "strategy":    "strategy_enabled",
        }
        col = col_map.get(category)
        if col:
            conn.execute(
                f"UPDATE subscriptions SET {col}=0 WHERE chat_id=?",
                (chat_id,),
            )
        # If all three are now 0, mark overall subscribed=0
        row = conn.execute(
            "SELECT disclosures_enabled, news_enabled, strategy_enabled"
            "  FROM subscriptions WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
        if row and not any([row["disclosures_enabled"], row["news_enabled"], row["strategy_enabled"]]):
            conn.execute("UPDATE subscriptions SET subscribed=0 WHERE chat_id=?", (chat_id,))


def get_subscription(chat_id: int) -> dict:
    """Return full subscription state for a chat, or defaults if not found."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT subscribed, disclosures_enabled, news_enabled, strategy_enabled, language"
            "  FROM subscriptions WHERE chat_id=?",
            (chat_id,),
        ).fetchone()
    if not row:
        return {"subscribed": 0, "disclosures_enabled": 0, "news_enabled": 0,
                "strategy_enabled": 0, "language": "en"}
    return dict(row)


def is_subscribed(chat_id: int) -> bool:
    return bool(get_subscription(chat_id).get("subscribed", 0))


def get_chats_for_category(category: str) -> list[dict]:
    """
    Return list of {chat_id, language} for chats subscribed to this category.
    category: disclosures | news | strategy
    """
    col_map = {
        "disclosures": "disclosures_enabled",
        "news":        "news_enabled",
        "strategy":    "strategy_enabled",
    }
    col = col_map.get(category, "disclosures_enabled")
    with get_conn() as conn:
        rows = conn.execute(
            f"SELECT chat_id, language FROM subscriptions"
            f"  WHERE subscribed=1 AND {col}=1",
        ).fetchall()
    return [dict(r) for r in rows]


# ── Language preference ────────────────────────────────────────────────────────

def set_lang(chat_id: int, lang: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO user_lang(chat_id, lang) VALUES(?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET lang=?",
            (chat_id, lang, lang)
        )
        # Keep subscriptions.language in sync too
        conn.execute(
            "UPDATE subscriptions SET language=? WHERE chat_id=?",
            (lang, chat_id),
        )


def get_lang(chat_id: int) -> str:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT lang FROM user_lang WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return row["lang"] if row else "en"


# ── Deduplication ──────────────────────────────────────────────────────────────

def is_new_disclosure(rcept_no: str, company: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_disclosures WHERE rcept_no=?", (rcept_no,)
        ).fetchone()
        if row:
            return False
        conn.execute(
            "INSERT INTO seen_disclosures(rcept_no, company) VALUES(?,?)",
            (rcept_no, company)
        )
    return True


def is_new_news(url: str, company: str) -> bool:
    h = hashlib.md5(url.encode()).hexdigest()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_news WHERE url_hash=?", (h,)
        ).fetchone()
        if row:
            return False
        conn.execute(
            "INSERT INTO seen_news(url_hash, company) VALUES(?,?)",
            (h, company)
        )
    return True


def is_new_strategy(identifier: str, company: str) -> bool:
    """Deduplicate strategy alerts by a stable identifier (rcept_no or url hash)."""
    h = hashlib.md5(identifier.encode()).hexdigest()
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_strategy WHERE item_hash=?", (h,)
        ).fetchone()
        if row:
            return False
        conn.execute(
            "INSERT INTO seen_strategy(item_hash, company) VALUES(?,?)",
            (h, company)
        )
    return True


# ── Audit log ─────────────────────────────────────────────────────────────────

def log_event(
    event_type: str,
    user_id: int | None,
    username: str | None,
    chat_id: int | None,
    payload: str = "",
):
    """Write one row to audit_log. Never raises — errors are logged only."""
    try:
        ts = datetime.now(SEOUL).strftime("%Y-%m-%d %H:%M:%S")
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO audit_log"
                "  (timestamp, event_type, user_id, username, chat_id, payload)"
                "  VALUES (?,?,?,?,?,?)",
                (ts, event_type, user_id, username or "", chat_id, (payload or "")[:500]),
            )
    except Exception as exc:
        log.warning("audit log_event failed: %s", exc)


def get_recent_audit(limit: int = 20) -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT timestamp, event_type, user_id, username, chat_id, payload"
            "  FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()


def get_recent_users(limit: int = 20) -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT username, user_id, MAX(timestamp) AS last_seen"
            "  FROM audit_log WHERE user_id IS NOT NULL"
            "  GROUP BY user_id ORDER BY last_seen DESC LIMIT ?",
            (limit,),
        ).fetchall()