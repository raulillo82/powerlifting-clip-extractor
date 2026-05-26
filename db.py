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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS job_stats (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                submitted_at  REAL    NOT NULL,
                started_at    REAL,
                finished_at   REAL,
                status        TEXT    NOT NULL,
                source        TEXT,
                mode          TEXT,
                has_music     INTEGER NOT NULL DEFAULT 0,
                city          TEXT,
                country_code  TEXT,
                latitude      REAL,
                longitude     REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id      INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                issue_number INTEGER NOT NULL,
                issue_url    TEXT NOT NULL,
                title        TEXT NOT NULL DEFAULT '',
                body_excerpt TEXT NOT NULL,
                submitted_at REAL NOT NULL,
                status       TEXT NOT NULL DEFAULT 'open',
                labels       TEXT NOT NULL DEFAULT '[]'
            )
        """)
        # Migration: add title column to existing DBs
        try:
            conn.execute("ALTER TABLE feedback ADD COLUMN title TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
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


def record_job_stat(
    submitted_at: float,
    started_at: float | None,
    finished_at: float | None,
    status: str,
    source: str | None = None,
    mode: str | None = None,
    has_music: int = 0,
    city: str | None = None,
    country_code: str | None = None,
    latitude: float | None = None,
    longitude: float | None = None,
) -> None:
    with get_db() as conn:
        conn.execute("""
            INSERT INTO job_stats
                (submitted_at, started_at, finished_at, status, source, mode,
                 has_music, city, country_code, latitude, longitude)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (submitted_at, started_at, finished_at, status, source, mode,
              has_music, city, country_code, latitude, longitude))
        conn.commit()


def get_stats(days: int | None) -> dict:
    cutoff = time.time() - days * 86400 if days else 0
    with get_db() as conn:
        summary = conn.execute("""
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END), 0) AS success,
                COALESCE(AVG(CASE WHEN finished_at IS NOT NULL AND started_at IS NOT NULL
                                  THEN finished_at - started_at END), 0) AS avg_duration
            FROM job_stats WHERE submitted_at >= ?
        """, (cutoff,)).fetchone()

        by_status = conn.execute("""
            SELECT status, COUNT(*) AS n
            FROM job_stats WHERE submitted_at >= ?
            GROUP BY status
        """, (cutoff,)).fetchall()

        by_source = conn.execute("""
            SELECT COALESCE(source, 'custom') AS source, COUNT(*) AS n
            FROM job_stats WHERE submitted_at >= ?
            GROUP BY source ORDER BY n DESC
        """, (cutoff,)).fetchall()

        by_mode = conn.execute("""
            SELECT COALESCE(mode, 'full') AS mode, COUNT(*) AS n
            FROM job_stats WHERE submitted_at >= ?
            GROUP BY mode
        """, (cutoff,)).fetchall()

        by_music = conn.execute("""
            SELECT has_music, COUNT(*) AS n
            FROM job_stats WHERE submitted_at >= ?
            GROUP BY has_music
        """, (cutoff,)).fetchall()

        by_hour_raw = conn.execute("""
            SELECT
                CAST(strftime('%H', datetime(submitted_at, 'unixepoch', 'localtime')) AS INTEGER) AS hour,
                COUNT(*) AS n
            FROM job_stats WHERE submitted_at >= ?
            GROUP BY hour ORDER BY hour
        """, (cutoff,)).fetchall()

        by_city = conn.execute("""
            SELECT city, country_code,
                   AVG(latitude) AS lat, AVG(longitude) AS lng,
                   COUNT(*) AS n
            FROM job_stats
            WHERE submitted_at >= ? AND city IS NOT NULL
            GROUP BY city ORDER BY n DESC
        """, (cutoff,)).fetchall()

    hour_map = {r["hour"]: r["n"] for r in by_hour_raw}
    by_hour = [hour_map.get(h, 0) for h in range(24)]

    return {
        "summary": dict(summary),
        "by_status": [dict(r) for r in by_status],
        "by_source": [dict(r) for r in by_source],
        "by_mode": [dict(r) for r in by_mode],
        "by_music": [dict(r) for r in by_music],
        "by_hour": by_hour,
        "by_city": [dict(r) for r in by_city],
    }


def add_feedback(user_id: int, issue_number: int, issue_url: str,
                 title: str, body_excerpt: str) -> int:
    with get_db() as conn:
        cur = conn.execute("""
            INSERT INTO feedback (user_id, issue_number, issue_url, title, body_excerpt, submitted_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (user_id, issue_number, issue_url, title, body_excerpt, time.time()))
        conn.commit()
        return cur.lastrowid


def get_feedback_for_user(user_id: int) -> list:
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM feedback WHERE user_id = ? ORDER BY submitted_at DESC
        """, (user_id,)).fetchall()
    return [dict(r) for r in rows]


def get_all_feedback(status: str | None = None) -> list:
    with get_db() as conn:
        if status:
            rows = conn.execute("""
                SELECT f.*, u.display_name
                FROM feedback f JOIN users u ON f.user_id = u.id
                WHERE f.status = ?
                ORDER BY f.submitted_at DESC
            """, (status,)).fetchall()
        else:
            rows = conn.execute("""
                SELECT f.*, u.display_name
                FROM feedback f JOIN users u ON f.user_id = u.id
                ORDER BY f.submitted_at DESC
            """).fetchall()
    return [dict(r) for r in rows]


def update_feedback_from_github(issue_number: int, status: str, labels_json: str) -> None:
    with get_db() as conn:
        conn.execute("""
            UPDATE feedback SET status = ?, labels = ? WHERE issue_number = ?
        """, (status, labels_json, issue_number))
        conn.commit()


def get_all_staging_access() -> dict[int, float]:
    """Return {user_id: expires_at} for all entries."""
    with get_db() as conn:
        rows = conn.execute("SELECT user_id, expires_at FROM staging_access").fetchall()
    return {r["user_id"]: r["expires_at"] for r in rows}
