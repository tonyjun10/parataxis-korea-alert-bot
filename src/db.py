"""
db.py — Postgres persistence layer (v7)

Migrated from SQLite to Postgres for Railway deployment persistence.
Uses DATABASE_URL env var injected by Railway Postgres plugin.
Falls back to SQLite if DATABASE_URL is not set (local dev).

Changes from v6:
  - psycopg2 used when DATABASE_URL is set; sqlite3 fallback otherwise
  - ? placeholders → %s for Postgres
  - INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING
  - INSERT OR REPLACE → INSERT ... ON CONFLICT DO UPDATE
  - executescript removed; individual execute calls used
  - PRAGMA migrations removed (clean Postgres schema)
  - Row access unified via dict cursor
"""

import hashlib
import logging
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

log   = logging.getLogger(__name__)
SEOUL = ZoneInfo("Asia/Seoul")

COMPANIES      = ["parataxis", "bitmax", "bitplanet", "microstrategy"]
DART_COMPANIES = {"parataxis", "bitmax", "bitplanet"}

DATABASE_URL = os.environ.get("DATABASE_URL", "")
DB_PATH      = Path(os.environ.get("DB_PATH", "data/bot.db"))

USE_POSTGRES = bool(DATABASE_URL)

if USE_POSTGRES:
    import psycopg2
    import psycopg2.extras


# ── Connection ────────────────────────────────────────────────────────────────

def get_conn():
    if USE_POSTGRES:
        conn = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
        conn.autocommit = False
        return conn
    else:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn


# ── SQL dialect helpers ───────────────────────────────────────────────────────

def _ph(n: int = 1) -> str:
    p = "%s" if USE_POSTGRES else "?"
    return ", ".join([p] * n)


def _p() -> str:
    return "%s" if USE_POSTGRES else "?"


def _execute(conn, sql: str, params=()):
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur


def _executemany(conn, sql: str, params_list):
    cur = conn.cursor()
    cur.executemany(sql, params_list)
    return cur


def _fetchone(cur):
    row = cur.fetchone()
    return dict(row) if row is not None else None


def _fetchall(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


# ── Schema ────────────────────────────────────────────────────────────────────

_POSTGRES_TABLES = [
    """CREATE TABLE IF NOT EXISTS approved_chats (
        chat_id     BIGINT PRIMARY KEY,
        approved    INTEGER NOT NULL DEFAULT 0,
        approved_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS user_lang (
        chat_id BIGINT PRIMARY KEY,
        lang    TEXT NOT NULL DEFAULT 'en'
    )""",
    """CREATE TABLE IF NOT EXISTS subscriptions (
        chat_id  BIGINT NOT NULL,
        company  TEXT   NOT NULL,
        category TEXT   NOT NULL,
        PRIMARY KEY (chat_id, company, category)
    )""",
    """CREATE TABLE IF NOT EXISTS seen_disclosures (
        rcept_no TEXT NOT NULL,
        company  TEXT NOT NULL,
        seen_at  TEXT DEFAULT (NOW()::TEXT),
        PRIMARY KEY (rcept_no, company)
    )""",
    """CREATE TABLE IF NOT EXISTS seen_news (
        url_hash TEXT NOT NULL,
        company  TEXT NOT NULL,
        seen_at  TEXT DEFAULT (NOW()::TEXT),
        PRIMARY KEY (url_hash, company)
    )""",
    """CREATE TABLE IF NOT EXISTS audit_log (
        id         SERIAL PRIMARY KEY,
        timestamp  TEXT NOT NULL,
        event_type TEXT NOT NULL,
        user_id    BIGINT,
        username   TEXT,
        chat_id    BIGINT,
        payload    TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS user_t_lang (
        user_id BIGINT PRIMARY KEY,
        lang    TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS user_t_lang (
    user_id INTEGER PRIMARY KEY,
    lang    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kakao_log (
        id        SERIAL PRIMARY KEY,
        logged_at TEXT NOT NULL,
        user_id   BIGINT,
        username  TEXT,
        message   TEXT NOT NULL
    )""",
]

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS approved_chats (
    chat_id     INTEGER PRIMARY KEY,
    approved    INTEGER NOT NULL DEFAULT 0,
    approved_at TEXT
);
CREATE TABLE IF NOT EXISTS user_lang (
    chat_id INTEGER PRIMARY KEY,
    lang    TEXT NOT NULL DEFAULT 'en'
);
CREATE TABLE IF NOT EXISTS subscriptions (
    chat_id  INTEGER NOT NULL,
    company  TEXT    NOT NULL,
    category TEXT    NOT NULL,
    PRIMARY KEY (chat_id, company, category)
);
CREATE TABLE IF NOT EXISTS seen_disclosures (
    rcept_no TEXT NOT NULL,
    company  TEXT NOT NULL,
    seen_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (rcept_no, company)
);
CREATE TABLE IF NOT EXISTS seen_news (
    url_hash TEXT NOT NULL,
    company  TEXT NOT NULL,
    seen_at  TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (url_hash, company)
);
CREATE TABLE IF NOT EXISTS audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  TEXT NOT NULL,
    event_type TEXT NOT NULL,
    user_id    INTEGER,
    username   TEXT,
    chat_id    INTEGER,
    payload    TEXT
);
CREATE TABLE IF NOT EXISTS kakao_log (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    logged_at TEXT NOT NULL,
    user_id   INTEGER,
    username  TEXT,
    message   TEXT NOT NULL
);
"""


def init_db():
    conn = get_conn()
    try:
        if USE_POSTGRES:
            for stmt in _POSTGRES_TABLES:
                _execute(conn, stmt)
        else:
            conn.executescript(_SQLITE_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    log.info("DB init complete (%s)", "postgres" if USE_POSTGRES else DB_PATH)


# ── URL normalisation ──────────────────────────────────────────────────────────

def _normalise_url(url: str) -> str:
    url = url.strip()
    url = re.sub(r'[?&](utm_[^&]+|source=[^&]+|ref=[^&]+)', '', url)
    url = re.sub(r'\?&', '?', url).rstrip('?&')
    return url


# ── Access control ─────────────────────────────────────────────────────────────

def is_approved(chat_id: int) -> bool:
    conn = get_conn()
    try:
        cur = _execute(conn,
            f"SELECT approved FROM approved_chats WHERE chat_id={_p()}",
            (chat_id,))
        row = _fetchone(cur)
        return bool(row and row["approved"])
    finally:
        conn.close()


def request_access(chat_id: int):
    conn = get_conn()
    try:
        if USE_POSTGRES:
            _execute(conn,
                "INSERT INTO approved_chats(chat_id, approved) VALUES(%s, 0) "
                "ON CONFLICT (chat_id) DO NOTHING", (chat_id,))
        else:
            _execute(conn,
                "INSERT OR IGNORE INTO approved_chats(chat_id, approved) VALUES(?, 0)",
                (chat_id,))
        conn.commit()
    finally:
        conn.close()


def approve_chat(chat_id: int):
    ts = datetime.now(SEOUL).strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()
    try:
        if USE_POSTGRES:
            _execute(conn,
                "INSERT INTO approved_chats(chat_id, approved, approved_at) VALUES(%s, 1, %s) "
                "ON CONFLICT (chat_id) DO UPDATE SET approved=1, approved_at=%s",
                (chat_id, ts, ts))
        else:
            _execute(conn,
                "INSERT INTO approved_chats(chat_id, approved, approved_at) VALUES(?,1,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET approved=1, approved_at=?",
                (chat_id, ts, ts))
        conn.commit()
    finally:
        conn.close()


def deny_chat(chat_id: int):
    conn = get_conn()
    try:
        if USE_POSTGRES:
            _execute(conn,
                "INSERT INTO approved_chats(chat_id, approved) VALUES(%s, 0) "
                "ON CONFLICT (chat_id) DO UPDATE SET approved=0", (chat_id,))
        else:
            _execute(conn,
                "INSERT INTO approved_chats(chat_id, approved) VALUES(?,0) "
                "ON CONFLICT(chat_id) DO UPDATE SET approved=0", (chat_id,))
        conn.commit()
    finally:
        conn.close()


def get_pending_requests() -> list[dict]:
    conn = get_conn()
    try:
        cur = _execute(conn, "SELECT chat_id FROM approved_chats WHERE approved=0")
        return _fetchall(cur)
    finally:
        conn.close()


def get_approved_chats() -> list[int]:
    """Return list of all approved chat_ids."""
    conn = get_conn()
    try:
        cur = _execute(conn, "SELECT chat_id FROM approved_chats WHERE approved=1")
        return [row["chat_id"] for row in _fetchall(cur)]
    finally:
        conn.close()


# ── Subscriptions ──────────────────────────────────────────────────────────────

def subscribe(chat_id: int, company: str, category: str):
    conn = get_conn()
    try:
        if USE_POSTGRES:
            _execute(conn,
                "INSERT INTO subscriptions(chat_id, company, category) VALUES(%s, %s, %s) "
                "ON CONFLICT (chat_id, company, category) DO NOTHING",
                (chat_id, company, category))
        else:
            _execute(conn,
                "INSERT OR IGNORE INTO subscriptions(chat_id, company, category) VALUES(?,?,?)",
                (chat_id, company, category))
        conn.commit()
    finally:
        conn.close()


def subscribe_default(chat_id: int):
    entries = []
    for company in ["parataxis", "bitmax", "bitplanet"]:
        entries.append((chat_id, company, "news"))
        entries.append((chat_id, company, "disclosures"))
    entries.append((chat_id, "microstrategy", "news"))
    conn = get_conn()
    try:
        if USE_POSTGRES:
            _executemany(conn,
                "INSERT INTO subscriptions(chat_id, company, category) VALUES(%s, %s, %s) "
                "ON CONFLICT (chat_id, company, category) DO NOTHING", entries)
        else:
            _executemany(conn,
                "INSERT OR IGNORE INTO subscriptions(chat_id, company, category) VALUES(?,?,?)",
                entries)
        conn.commit()
    finally:
        conn.close()


def unsubscribe(chat_id: int, company: str | None = None, category: str | None = None):
    p = _p()
    conn = get_conn()
    try:
        if company is None and category is None:
            _execute(conn, f"DELETE FROM subscriptions WHERE chat_id={p}", (chat_id,))
        elif company is None:
            _execute(conn,
                f"DELETE FROM subscriptions WHERE chat_id={p} AND category={p}",
                (chat_id, category))
        elif category is None:
            _execute(conn,
                f"DELETE FROM subscriptions WHERE chat_id={p} AND company={p}",
                (chat_id, company))
        else:
            _execute(conn,
                f"DELETE FROM subscriptions WHERE chat_id={p} AND company={p} AND category={p}",
                (chat_id, company, category))
        conn.commit()
    finally:
        conn.close()


def get_subscriptions(chat_id: int) -> list[dict]:
    conn = get_conn()
    try:
        cur = _execute(conn,
            f"SELECT company, category FROM subscriptions "
            f"WHERE chat_id={_p()} ORDER BY company, category",
            (chat_id,))
        return _fetchall(cur)
    finally:
        conn.close()


def has_any_subscription(chat_id: int) -> bool:
    conn = get_conn()
    try:
        cur = _execute(conn,
            f"SELECT 1 FROM subscriptions WHERE chat_id={_p()} LIMIT 1",
            (chat_id,))
        return _fetchone(cur) is not None
    finally:
        conn.close()


def get_chats_for(company: str, category: str) -> list[dict]:
    """Return only private DM chats (chat_id > 0) — group chats never receive push notifications."""
    conn = get_conn()
    try:
        cur = _execute(conn,
            f"""SELECT s.chat_id, COALESCE(ul.lang, 'en') AS lang
            FROM subscriptions s
            LEFT JOIN user_lang ul ON ul.chat_id = s.chat_id
            WHERE s.company={_p()} AND s.category={_p()} AND s.chat_id > 0""",
            (company, category))
        return _fetchall(cur)
    finally:
        conn.close()


# ── Deduplication ──────────────────────────────────────────────────────────────

def is_new_disclosure(rcept_no: str, company: str) -> bool:
    conn = get_conn()
    try:
        cur = _execute(conn,
            f"SELECT 1 FROM seen_disclosures WHERE rcept_no={_p()} AND company={_p()}",
            (rcept_no, company))
        if _fetchone(cur):
            return False
        if USE_POSTGRES:
            _execute(conn,
                "INSERT INTO seen_disclosures(rcept_no, company) VALUES(%s, %s) "
                "ON CONFLICT (rcept_no, company) DO NOTHING", (rcept_no, company))
        else:
            _execute(conn,
                "INSERT OR IGNORE INTO seen_disclosures(rcept_no, company) VALUES(?,?)",
                (rcept_no, company))
        conn.commit()
        return True
    finally:
        conn.close()


def mark_disclosure_seen(rcept_no: str, company: str):
    conn = get_conn()
    try:
        if USE_POSTGRES:
            _execute(conn,
                "INSERT INTO seen_disclosures(rcept_no, company) VALUES(%s, %s) "
                "ON CONFLICT (rcept_no, company) DO NOTHING", (rcept_no, company))
        else:
            _execute(conn,
                "INSERT OR IGNORE INTO seen_disclosures(rcept_no, company) VALUES(?,?)",
                (rcept_no, company))
        conn.commit()
    finally:
        conn.close()


def is_new_news(url: str, company: str) -> bool:
    h = hashlib.md5(_normalise_url(url).encode()).hexdigest()
    conn = get_conn()
    try:
        cur = _execute(conn,
            f"SELECT 1 FROM seen_news WHERE url_hash={_p()} AND company={_p()}",
            (h, company))
        if _fetchone(cur):
            return False
        if USE_POSTGRES:
            _execute(conn,
                "INSERT INTO seen_news(url_hash, company) VALUES(%s, %s) "
                "ON CONFLICT (url_hash, company) DO NOTHING", (h, company))
        else:
            _execute(conn,
                "INSERT OR IGNORE INTO seen_news(url_hash, company) VALUES(?,?)",
                (h, company))
        conn.commit()
        return True
    finally:
        conn.close()


def mark_news_seen(url: str, company: str):
    h = hashlib.md5(_normalise_url(url).encode()).hexdigest()
    conn = get_conn()
    try:
        if USE_POSTGRES:
            _execute(conn,
                "INSERT INTO seen_news(url_hash, company) VALUES(%s, %s) "
                "ON CONFLICT (url_hash, company) DO NOTHING", (h, company))
        else:
            _execute(conn,
                "INSERT OR IGNORE INTO seen_news(url_hash, company) VALUES(?,?)",
                (h, company))
        conn.commit()
    finally:
        conn.close()


def seed_seen_for_chat(
    news_items_by_company: dict[str, list[dict]],
    disclosure_items_by_company: dict[str, list[dict]],
):
    for company, items in news_items_by_company.items():
        for it in items:
            url = it.get("url", "")
            if url:
                mark_news_seen(url, company)
        log.info("Seeded %d news items as seen for company=%s", len(items), company)
    for company, items in disclosure_items_by_company.items():
        for it in items:
            rcept_no = it.get("rcept_no", "")
            if rcept_no and "error" not in it:
                mark_disclosure_seen(rcept_no, company)
        log.info("Seeded %d disclosures as seen for company=%s", len(items), company)


# ── Language ───────────────────────────────────────────────────────────────────

def set_lang(chat_id: int, lang: str):
    conn = get_conn()
    try:
        if USE_POSTGRES:
            _execute(conn,
                "INSERT INTO user_lang(chat_id, lang) VALUES(%s, %s) "
                "ON CONFLICT (chat_id) DO UPDATE SET lang=%s",
                (chat_id, lang, lang))
        else:
            _execute(conn,
                "INSERT INTO user_lang(chat_id, lang) VALUES(?,?) "
                "ON CONFLICT(chat_id) DO UPDATE SET lang=?",
                (chat_id, lang, lang))
        conn.commit()
    finally:
        conn.close()


def get_lang(chat_id: int) -> str:
    conn = get_conn()
    try:
        cur = _execute(conn,
            f"SELECT lang FROM user_lang WHERE chat_id={_p()}",
            (chat_id,))
        row = _fetchone(cur)
        return row["lang"] if row else "en"
    finally:
        conn.close()


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
        conn = get_conn()
        try:
            if USE_POSTGRES:
                _execute(conn,
                    "INSERT INTO audit_log(timestamp, event_type, user_id, username, chat_id, payload)"
                    " VALUES(%s, %s, %s, %s, %s, %s)",
                    (ts, event_type, user_id, username or "", chat_id, (payload or "")[:500]))
            else:
                _execute(conn,
                    "INSERT INTO audit_log(timestamp, event_type, user_id, username, chat_id, payload)"
                    " VALUES(?,?,?,?,?,?)",
                    (ts, event_type, user_id, username or "", chat_id, (payload or "")[:500]))
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        log.warning("audit log_event failed: %s", exc)


def get_recent_audit(limit: int = 20) -> list:
    conn = get_conn()
    try:
        cur = _execute(conn,
            f"SELECT timestamp, event_type, user_id, username, chat_id, payload"
            f" FROM audit_log ORDER BY id DESC LIMIT {_p()}",
            (limit,))
        return _fetchall(cur)
    finally:
        conn.close()


def get_recent_users(limit: int = 20) -> list:
    conn = get_conn()
    try:
        cur = _execute(conn,
            f"SELECT username, user_id, MAX(timestamp) AS last_seen"
            f" FROM audit_log WHERE user_id IS NOT NULL"
            f" GROUP BY user_id, username ORDER BY last_seen DESC LIMIT {_p()}",
            (limit,))
        return _fetchall(cur)
    finally:
        conn.close()


# ── Kakao log ──────────────────────────────────────────────────────────────────

def kakao_log_add(user_id: int, username: str | None, message: str) -> None:
    """Insert a new kakao log entry."""
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    ts = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M KST")
    conn = get_conn()
    try:
        _execute(conn,
            f"INSERT INTO kakao_log (logged_at, user_id, username, message) VALUES ({_p()},{_p()},{_p()},{_p()})",
            (ts, user_id, username, message))
        conn.commit()
    finally:
        conn.close()


def kakao_log_get_all() -> list[dict]:
    """Return all kakao log entries, oldest first."""
    conn = get_conn()
    try:
        cur = _execute(conn, "SELECT logged_at, user_id, username, message FROM kakao_log ORDER BY id ASC")
        return _fetchall(cur)
    finally:
        conn.close()


def kakao_log_get_recent(limit: int = 3) -> list[dict]:
    """Return the most recent kakao log entries, oldest-first within the slice."""
    conn = get_conn()
    try:
        cur = _execute(conn,
            f"SELECT logged_at, user_id, username, message FROM kakao_log ORDER BY id DESC LIMIT {_p()}",
            (limit,))
        return list(reversed(_fetchall(cur)))
    finally:
        conn.close()


# ── User translation language preference ──────────────────────────────────────

def set_t_lang(user_id: int, lang: str) -> None:
    """Save a user's default translation target language."""
    conn = get_conn()
    try:
        if USE_POSTGRES:
            _execute(conn,
                "INSERT INTO user_t_lang(user_id, lang) VALUES(%s, %s) "
                "ON CONFLICT (user_id) DO UPDATE SET lang=%s",
                (user_id, lang, lang))
        else:
            _execute(conn,
                "INSERT INTO user_t_lang(user_id, lang) VALUES(?,?) "
                "ON CONFLICT(user_id) DO UPDATE SET lang=?",
                (user_id, lang, lang))
        conn.commit()
    finally:
        conn.close()


def get_t_lang(user_id: int) -> str | None:
    """Return the user's saved translation target language, or None."""
    conn = get_conn()
    try:
        cur = _execute(conn,
            f"SELECT lang FROM user_t_lang WHERE user_id={_p()}",
            (user_id,))
        row = cur.fetchone()
        if row:
            return row["lang"] if isinstance(row, dict) else row[0]
        return None
    finally:
        conn.close()


def unset_t_lang(user_id: int) -> None:
    """Remove the user's saved translation target language."""
    conn = get_conn()
    try:
        _execute(conn,
            f"DELETE FROM user_t_lang WHERE user_id={_p()}",
            (user_id,))
        conn.commit()
    finally:
        conn.close()


def get_all_subscriptions() -> list[dict]:
    """Return all subscriptions grouped with username where available."""
    conn = get_conn()
    try:
        cur = _execute(conn,
            f"""SELECT s.chat_id, s.company, s.category,
                COALESCE(ul.lang, 'en') AS lang
                FROM subscriptions s
                LEFT JOIN user_lang ul ON ul.chat_id = s.chat_id
                WHERE s.chat_id > 0
                ORDER BY s.chat_id, s.company, s.category""")
        return _fetchall(cur)
    finally:
        conn.close()
