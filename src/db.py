"""
db.py — SQLite persistence layer

Schema (v5):
  approved_chats  — access control (replaces whitelist env var)
  user_lang       — language preference per chat
  subscriptions   — one row per (chat_id, company, category); many-to-many
  seen_disclosures — dedup by rcept_no
  seen_news        — dedup by url hash
  audit_log        — admin audit trail
"""

import hashlib
import logging
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path("data/bot.db")
SEOUL   = ZoneInfo("Asia/Seoul")
log     = logging.getLogger(__name__)

# Canonical company keys used throughout the codebase
COMPANIES = ["parataxis", "bitmax", "bitplanet", "microstrategy"]

# Companies that support DART disclosures
DART_COMPANIES = {"parataxis", "bitmax", "bitplanet"}


def get_conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            -- Access control: approved chat IDs
            CREATE TABLE IF NOT EXISTS approved_chats (
                chat_id    INTEGER PRIMARY KEY,
                approved   INTEGER NOT NULL DEFAULT 0,
                approved_at TEXT
            );

            -- Language preference per chat
            CREATE TABLE IF NOT EXISTS user_lang (
                chat_id  INTEGER PRIMARY KEY,
                lang     TEXT NOT NULL DEFAULT 'en'
            );

            -- Normalised subscriptions: one row per (chat_id, company, category)
            CREATE TABLE IF NOT EXISTS subscriptions (
                chat_id   INTEGER NOT NULL,
                company   TEXT    NOT NULL,
                category  TEXT    NOT NULL,
                PRIMARY KEY (chat_id, company, category)
            );

            -- Deduplication tables
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

            -- Audit log
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
    log.info("DB init complete at %s", DB_PATH)


def _migrate(conn: sqlite3.Connection):
    """
    Safe migration from old single-row subscriptions schema (if it exists).
    Reads old bitmask rows and converts them to the new normalised format.
    Idempotent — safe to run multiple times.
    """
    old_cols = {row[1] for row in conn.execute("PRAGMA table_info(subscriptions)")}

    if "chat_id" in old_cols and "company" not in old_cols:
        # Old schema detected — migrate data
        log.info("Migration: converting old subscriptions schema to normalised format.")
        rows = conn.execute(
            "SELECT chat_id, subscribed, disclosures_enabled, news_enabled, language "
            "FROM subscriptions WHERE subscribed=1"
        ).fetchall()

        # Rename old table, recreate new one
        conn.executescript("""
            ALTER TABLE subscriptions RENAME TO subscriptions_old;
            CREATE TABLE IF NOT EXISTS subscriptions (
                chat_id   INTEGER NOT NULL,
                company   TEXT    NOT NULL,
                category  TEXT    NOT NULL,
                PRIMARY KEY (chat_id, company, category)
            );
        """)

        for row in rows:
            cid = row["chat_id"]
            # Migrate language
            lang = row.get("language", "en") or "en"
            conn.execute(
                "INSERT OR REPLACE INTO user_lang(chat_id, lang) VALUES(?,?)",
                (cid, lang)
            )
            # Migrate approval
            conn.execute(
                "INSERT OR IGNORE INTO approved_chats(chat_id, approved) VALUES(?,1)",
                (cid,)
            )
            # Migrate subscriptions — default: subscribe all companies/categories
            for company in COMPANIES:
                for category in ["news", "disclosures"]:
                    if category == "disclosures" and company not in DART_COMPANIES:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO subscriptions(chat_id, company, category) VALUES(?,?,?)",
                        (cid, company, category)
                    )

        conn.execute("DROP TABLE IF EXISTS subscriptions_old")
        log.info("Migration complete: %d chats migrated.", len(rows))

    # Ensure approved_chats exists (handles partial old schema cases)
    try:
        conn.execute("SELECT 1 FROM approved_chats LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS approved_chats (
                chat_id    INTEGER PRIMARY KEY,
                approved   INTEGER NOT NULL DEFAULT 0,
                approved_at TEXT
            )
        """)


# ── Access control ─────────────────────────────────────────────────────────────

def is_approved(chat_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT approved FROM approved_chats WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return bool(row and row["approved"])


def request_access(chat_id: int):
    """Create a pending (unapproved) entry if one doesn't exist."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO approved_chats(chat_id, approved) VALUES(?,0)",
            (chat_id,)
        )


def approve_chat(chat_id: int):
    ts = datetime.now(SEOUL).strftime("%Y-%m-%d %H:%M:%S")
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO approved_chats(chat_id, approved, approved_at) VALUES(?,1,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET approved=1, approved_at=?",
            (chat_id, ts, ts)
        )


def deny_chat(chat_id: int):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO approved_chats(chat_id, approved) VALUES(?,0) "
            "ON CONFLICT(chat_id) DO UPDATE SET approved=0",
            (chat_id,)
        )


# ── Subscriptions (normalised many-to-many) ────────────────────────────────────

def subscribe(chat_id: int, company: str, category: str):
    """Add a single (chat_id, company, category) subscription row."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO subscriptions(chat_id, company, category) VALUES(?,?,?)",
            (chat_id, company, category)
        )


def subscribe_default(chat_id: int):
    """
    Default subscription when user runs /watch with no argument:
      - news + disclosures for parataxis, bitmax, bitplanet
      - news only for microstrategy
    """
    with get_conn() as conn:
        entries = []
        for company in ["parataxis", "bitmax", "bitplanet"]:
            entries.append((chat_id, company, "news"))
            entries.append((chat_id, company, "disclosures"))
        entries.append((chat_id, "microstrategy", "news"))
        conn.executemany(
            "INSERT OR IGNORE INTO subscriptions(chat_id, company, category) VALUES(?,?,?)",
            entries
        )


def unsubscribe(chat_id: int, company: str | None = None, category: str | None = None):
    """
    Remove subscriptions. If company and category are both None, removes all.
    If only category given, removes that category across all companies.
    If both given, removes the single row.
    """
    with get_conn() as conn:
        if company is None and category is None:
            conn.execute("DELETE FROM subscriptions WHERE chat_id=?", (chat_id,))
        elif company is None:
            conn.execute(
                "DELETE FROM subscriptions WHERE chat_id=? AND category=?",
                (chat_id, category)
            )
        elif category is None:
            conn.execute(
                "DELETE FROM subscriptions WHERE chat_id=? AND company=?",
                (chat_id, company)
            )
        else:
            conn.execute(
                "DELETE FROM subscriptions WHERE chat_id=? AND company=? AND category=?",
                (chat_id, company, category)
            )


def get_subscriptions(chat_id: int) -> list[dict]:
    """Return all subscription rows for a chat."""
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT company, category FROM subscriptions WHERE chat_id=? ORDER BY company, category",
            (chat_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def has_any_subscription(chat_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM subscriptions WHERE chat_id=? LIMIT 1", (chat_id,)
        ).fetchone()
    return row is not None


def get_chats_for(company: str, category: str) -> list[dict]:
    """
    Return [{chat_id, lang}] for all chats subscribed to (company, category).
    Joins with user_lang for the chat's language preference.
    """
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT s.chat_id, COALESCE(ul.lang, 'en') AS lang
            FROM subscriptions s
            LEFT JOIN user_lang ul ON ul.chat_id = s.chat_id
            WHERE s.company=? AND s.category=?
            """,
            (company, category)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Language ───────────────────────────────────────────────────────────────────

def set_lang(chat_id: int, lang: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO user_lang(chat_id, lang) VALUES(?,?) "
            "ON CONFLICT(chat_id) DO UPDATE SET lang=?",
            (chat_id, lang, lang)
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
        if conn.execute(
            "SELECT 1 FROM seen_disclosures WHERE rcept_no=?", (rcept_no,)
        ).fetchone():
            return False
        conn.execute(
            "INSERT INTO seen_disclosures(rcept_no, company) VALUES(?,?)",
            (rcept_no, company)
        )
    return True


def is_new_news(url: str, company: str) -> bool:
    h = hashlib.md5(url.encode()).hexdigest()
    with get_conn() as conn:
        if conn.execute(
            "SELECT 1 FROM seen_news WHERE url_hash=?", (h,)
        ).fetchone():
            return False
        conn.execute(
            "INSERT INTO seen_news(url_hash, company) VALUES(?,?)",
            (h, company)
        )
    return True


# ── Audit log ──────────────────────────────────────────────────────────────────

def log_event(
    event_type: str,
    user_id: int | None,
    username: str | None,
    chat_id: int | None,
    payload: str = "",
):
    try:
        ts = datetime.now(SEOUL).strftime("%Y-%m-%d %H:%M:%S")
        with get_conn() as conn:
            conn.execute(
                "INSERT INTO audit_log(timestamp, event_type, user_id, username, chat_id, payload)"
                " VALUES(?,?,?,?,?,?)",
                (ts, event_type, user_id, username or "", chat_id, (payload or "")[:500]),
            )
    except Exception as exc:
        log.warning("audit log_event failed: %s", exc)


def get_recent_audit(limit: int = 20) -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT timestamp, event_type, user_id, username, chat_id, payload"
            " FROM audit_log ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()


def get_recent_users(limit: int = 20) -> list:
    with get_conn() as conn:
        return conn.execute(
            "SELECT username, user_id, MAX(timestamp) AS last_seen"
            " FROM audit_log WHERE user_id IS NOT NULL"
            " GROUP BY user_id ORDER BY last_seen DESC LIMIT ?",
            (limit,),
        ).fetchall()
