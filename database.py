import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Optional

from config import DATABASE_PATH

logger = logging.getLogger(__name__)


def get_connection():
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    conn = get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def init_db():
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS link_sync_cache (
                id                      INTEGER PRIMARY KEY AUTOINCREMENT,
                parent_conversation_id  TEXT NOT NULL,
                linked_conversation_id  TEXT NOT NULL,
                last_checked_at         TIMESTAMP NOT NULL,
                last_posted_at          TIMESTAMP,
                created_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at              TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (parent_conversation_id, linked_conversation_id)
            );

            CREATE INDEX IF NOT EXISTS idx_link_sync_parent
                ON link_sync_cache(parent_conversation_id);
            CREATE INDEX IF NOT EXISTS idx_link_sync_linked
                ON link_sync_cache(linked_conversation_id);

            CREATE TABLE IF NOT EXISTS sync_log (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at           TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                status           TEXT NOT NULL,
                parents_checked  INTEGER NOT NULL DEFAULT 0,
                links_checked    INTEGER NOT NULL DEFAULT 0,
                comments_posted  INTEGER NOT NULL DEFAULT 0,
                duration_ms      INTEGER,
                details          TEXT
            );
        """)

    logger.info("Database initialized at %s", DATABASE_PATH)


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _parse_ts(value) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    # sqlite returns strings for TIMESTAMP columns
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def get_last_checked(parent_id: str, linked_id: str) -> Optional[datetime]:
    with get_db() as conn:
        row = conn.execute(
            """SELECT last_checked_at FROM link_sync_cache
               WHERE parent_conversation_id = ? AND linked_conversation_id = ?""",
            (parent_id, linked_id),
        ).fetchone()
        return _parse_ts(row["last_checked_at"]) if row else None


def upsert_last_checked(
    parent_id: str,
    linked_id: str,
    last_checked_at: datetime,
    last_posted_at: Optional[datetime] = None,
):
    checked = last_checked_at.astimezone(timezone.utc).isoformat()
    posted = last_posted_at.astimezone(timezone.utc).isoformat() if last_posted_at else None
    with get_db() as conn:
        conn.execute(
            """INSERT INTO link_sync_cache
                   (parent_conversation_id, linked_conversation_id,
                    last_checked_at, last_posted_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(parent_conversation_id, linked_conversation_id)
               DO UPDATE SET
                   last_checked_at = excluded.last_checked_at,
                   last_posted_at  = COALESCE(excluded.last_posted_at, last_posted_at),
                   updated_at      = CURRENT_TIMESTAMP""",
            (parent_id, linked_id, checked, posted),
        )


# ---------------------------------------------------------------------------
# Sync log
# ---------------------------------------------------------------------------

def record_sync_run(
    status: str,
    parents_checked: int,
    links_checked: int,
    comments_posted: int,
    duration_ms: int,
    details: Optional[str] = None,
):
    with get_db() as conn:
        conn.execute(
            """INSERT INTO sync_log
                   (status, parents_checked, links_checked, comments_posted,
                    duration_ms, details)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (status, parents_checked, links_checked, comments_posted, duration_ms, details),
        )


def get_last_sync():
    with get_db() as conn:
        row = conn.execute(
            "SELECT * FROM sync_log ORDER BY run_at DESC LIMIT 1",
        ).fetchone()
        return dict(row) if row else None
