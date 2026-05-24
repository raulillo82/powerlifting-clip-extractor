import sqlite3
import time
from pathlib import Path

DB_PATH = Path("users.db")


def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                username      TEXT    UNIQUE NOT NULL,
                display_name  TEXT    NOT NULL,
                password_hash TEXT    NOT NULL,
                is_admin      INTEGER NOT NULL DEFAULT 0,
                is_active     INTEGER NOT NULL DEFAULT 1,
                device_token  TEXT,
                created_at    REAL    NOT NULL DEFAULT (unixepoch())
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS staging_access (
                user_id    INTEGER PRIMARY KEY,
                expires_at REAL    NOT NULL
            )
        """)
        conn.commit()


def grant_staging_access(user_id: int, duration_seconds: int) -> None:
    expires_at = time.time() + duration_seconds
    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO staging_access (user_id, expires_at) VALUES (?, ?)",
            (user_id, expires_at),
        )
        conn.commit()


def revoke_staging_access(user_id: int) -> None:
    with get_db() as conn:
        conn.execute("DELETE FROM staging_access WHERE user_id = ?", (user_id,))
        conn.commit()


def get_staging_access(user_id: int) -> float | None:
    """Return expires_at unix timestamp, or None if no entry exists."""
    with get_db() as conn:
        row = conn.execute(
            "SELECT expires_at FROM staging_access WHERE user_id = ?", (user_id,)
        ).fetchone()
    return row["expires_at"] if row else None


def get_all_staging_access() -> dict[int, float]:
    """Return {user_id: expires_at} for all entries."""
    with get_db() as conn:
        rows = conn.execute("SELECT user_id, expires_at FROM staging_access").fetchall()
    return {r["user_id"]: r["expires_at"] for r in rows}
