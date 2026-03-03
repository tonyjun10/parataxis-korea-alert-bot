"""
db.py — SQLite persistence layer (v6)

Changes from v5:
  - seen_news dedup key is now (url_hash, company) composite — company-aware.
  - seen_disclosures dedup key is (rcept_no, company) composite — company-aware.
  - Added seed_seen_for_chat(): marks current top-N items as seen on first
    subscribe WITHOUT sending alerts (prevents backlog spam).
  - URL normalisation strips tracking params before hashing.
  - Schema uses composite PKs on both dedup tables.
"""

import hashlib
import logging
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

DB_PATH = Path("data/bot.db")
SEOUL   = ZoneInfo("Asia/Seoul")
log     = logging.getLogger(__name__)

COMPANIES     = ["parataxis", "bitmax", "bitplanet", "microstrategy"]
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
            CREATE TABLE IF NOT EXISTS approved_chats (
                chat_id     INTEGER PRIMARY KEY,
                approved    INTEGER NOT NULL DEFAULT 0,
                approved_at TEXT
            );

            CREATE TABLE IF NOT EXISTS user_lang (
                chat_id INTEGER PRIMARY KEY,
                lang    TEXT NOT NULL DEFAULT 'en'
            );

            -- Normalised: one row per (chat_id, company, category)
            CREATE TABLE IF NOT EXISTS subscriptions (
                chat_id  INTEGER NOT NULL,
                company  TEXT    NOT NULL,
                category TEXT    NOT NULL,
                PRIMARY KEY (chat_id, company, category)
            );

            -- Dedup: composite PK so same rcept_no for different companies is fine
            CREATE TABLE IF NOT EXISTS seen_disclosures (
                rcept_no TEXT NOT NULL,
                company  TEXT NOT NULL,
                seen_at  TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (rcept_no, company)
            );

            -- Dedup: composite PK (url_hash, company)
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
        """)
        _migrate(conn)
    log.info("DB init complete at %s", DB_PATH)


def _migrate(conn: sqlite3.Connection):
    """Idempotent migrations for older schema versions."""
    old_cols = {row[1] for row in conn.execute("PRAGMA table_info(subscriptions)")}

    # v4 → v5: flat bitmask subscriptions → normalised
    if "chat_id" in old_cols and "company" not in old_cols:
        log.info("Migration v4→v5: converting subscriptions schema.")
        rows = conn.execute(
            "SELECT chat_id, subscribed, disclosures_enabled, news_enabled, language "
            "FROM subscriptions WHERE subscribed=1"
        ).fetchall()
        conn.executescript("""
            ALTER TABLE subscriptions RENAME TO subscriptions_old;
            CREATE TABLE IF NOT EXISTS subscriptions (
                chat_id  INTEGER NOT NULL,
                company  TEXT    NOT NULL,
                category TEXT    NOT NULL,
                PRIMARY KEY (chat_id, company, category)
            );
        """)
        for row in rows:
            cid  = row["chat_id"]
            lang = row.get("language", "en") or "en"
            conn.execute(
                "INSERT OR REPLACE INTO user_lang(chat_id, lang) VALUES(?,?)", (cid, lang)
            )
            conn.execute(
                "INSERT OR IGNORE INTO approved_chats(chat_id, approved) VALUES(?,1)", (cid,)
            )
            for company in COMPANIES:
                for category in ["news", "disclosures"]:
                    if category == "disclosures" and company not in DART_COMPANIES:
                        continue
                    conn.execute(
                        "INSERT OR IGNORE INTO subscriptions(chat_id, company, category) VALUES(?,?,?)",
                        (cid, company, category)
                    )
        conn.execute("DROP TABLE IF EXISTS subscriptions_old")
        log.info("Migration v4→v5 complete: %d chats migrated.", len(rows))

    # v5 → v6: seen_news single PK → composite (url_hash, company)
    # Check if the old seen_news has a single-column PK
    seen_news_cols = {row[1] for row in conn.execute("PRAGMA table_info(seen_news)")}
    if "url_hash" in seen_news_cols and "company" in seen_news_cols:
        # Check if PK is composite by looking at pk column in pragma
        pk_cols = [
            row[1] for row in conn.execute("PRAGMA table_info(seen_news)") if row[5] > 0
        ]
        if pk_cols == ["url_hash"]:
            log.info("Migration v5→v6: upgrading seen_news to composite PK.")
            conn.executescript("""
                ALTER TABLE seen_news RENAME TO seen_news_old;
                CREATE TABLE IF NOT EXISTS seen_news (
                    url_hash TEXT NOT NULL,
                    company  TEXT NOT NULL,
                    seen_at  TEXT DEFAULT (datetime('now')),
                    PRIMARY KEY (url_hash, company)
                );
                INSERT OR IGNORE INTO seen_news(url_hash, company, seen_at)
                    SELECT url_hash, COALESCE(company, 'unknown'), seen_at
                    FROM seen_news_old;
                DROP TABLE seen_news_old;
            """)
            log.info("Migration v5→v6: seen_news upgraded.")

    # Similarly for seen_disclosures
    disc_pk_cols = [
        row[1] for row in conn.execute("PRAGMA table_info(seen_disclosures)") if row[5] > 0
    ]
    if disc_pk_cols == ["rcept_no"]:
        log.info("Migration v5→v6: upgrading seen_disclosures to composite PK.")
        conn.executescript("""
            ALTER TABLE seen_disclosures RENAME TO seen_disclosures_old;
            CREATE TABLE IF NOT EXISTS seen_disclosures (
                rcept_no TEXT NOT NULL,
                company  TEXT NOT NULL,
                seen_at  TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (rcept_no, company)
            );
            INSERT OR IGNORE INTO seen_disclosures(rcept_no, company, seen_at)
                SELECT rcept_no, COALESCE(company, 'unknown'), seen_at
                FROM seen_disclosures_old;
            DROP TABLE seen_disclosures_old;
        """)
        log.info("Migration v5→v6: seen_disclosures upgraded.")

    # Ensure approved_chats exists
    try:
        conn.execute("SELECT 1 FROM approved_chats LIMIT 1")
    except sqlite3.OperationalError:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS approved_chats (
                chat_id     INTEGER PRIMARY KEY,
                approved    INTEGER NOT NULL DEFAULT 0,
                approved_at TEXT
            )
        """)


# ── URL normalisation ──────────────────────────────────────────────────────────

def _normalise_url(url: str) -> str:
    """Strip common tracking parameters before hashing."""
    url = url.strip()
    # Remove UTM and other common tracking params
    url = re.sub(r'[?&](utm_[^&]+|source=[^&]+|ref=[^&]+)', '', url)
    # Collapse multiple ? or & artifacts
    url = re.sub(r'\?&', '?', url).rstrip('?&')
    return url


# ── Access control ─────────────────────────────────────────────────────────────

def is_approved(chat_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT approved FROM approved_chats WHERE chat_id=?", (chat_id,)
        ).fetchone()
    return bool(row and row["approved"])


def request_access(chat_id: int):
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


# ── Subscriptions ──────────────────────────────────────────────────────────────

def subscribe(chat_id: int, company: str, category: str):
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO subscriptions(chat_id, company, category) VALUES(?,?,?)",
            (chat_id, company, category)
        )


def subscribe_default(chat_id: int):
    """Subscribe to everything. Called on approval + /watch with no args."""
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
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT company, category FROM subscriptions "
            "WHERE chat_id=? ORDER BY company, category",
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
    """Return [{chat_id, lang}] for chats subscribed to (company, category)."""
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


# ── Deduplication ──────────────────────────────────────────────────────────────

def is_new_disclosure(rcept_no: str, company: str) -> bool:
    """
    Returns True and marks as seen if (rcept_no, company) is new.
    Company-aware: same rcept_no for different companies counts separately.
    """
    with get_conn() as conn:
        if conn.execute(
            "SELECT 1 FROM seen_disclosures WHERE rcept_no=? AND company=?",
            (rcept_no, company)
        ).fetchone():
            return False
        conn.execute(
            "INSERT OR IGNORE INTO seen_disclosures(rcept_no, company) VALUES(?,?)",
            (rcept_no, company)
        )
    return True


def mark_disclosure_seen(rcept_no: str, company: str):
    """Mark as seen without checking — used by seed_seen_for_chat."""
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_disclosures(rcept_no, company) VALUES(?,?)",
            (rcept_no, company)
        )


def is_new_news(url: str, company: str) -> bool:
    """
    Returns True and marks as seen if (normalised_url_hash, company) is new.
    Company-aware: same URL appearing for two companies is tracked separately.
    """
    h = hashlib.md5(_normalise_url(url).encode()).hexdigest()
    with get_conn() as conn:
        if conn.execute(
            "SELECT 1 FROM seen_news WHERE url_hash=? AND company=?",
            (h, company)
        ).fetchone():
            return False
        conn.execute(
            "INSERT OR IGNORE INTO seen_news(url_hash, company) VALUES(?,?)",
            (h, company)
        )
    return True


def mark_news_seen(url: str, company: str):
    """Mark as seen without checking — used by seed_seen_for_chat."""
    h = hashlib.md5(_normalise_url(url).encode()).hexdigest()
    with get_conn() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_news(url_hash, company) VALUES(?,?)",
            (h, company)
        )


def seed_seen_for_chat(
    news_items_by_company: dict[str, list[dict]],
    disclosure_items_by_company: dict[str, list[dict]],
):
    """
    Called once on first subscribe. Marks the current top-N items as seen
    so the first monitor run doesn't spam the newly subscribed chat.
    Does NOT send any alerts.
    """
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
