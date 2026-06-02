"""
database.py — SQLite config store for the SonarCloud Slack bot.

Schema:
  repos      — one row per registered repo
  file_paths — many rows per repo (the file path filters)
"""

import sqlite3
import os
from contextlib import contextmanager
from typing import Optional

DB_PATH = os.environ.get("DB_PATH", "sonar_bot.db")


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS repos (
                project_key  TEXT PRIMARY KEY,
                org_slug     TEXT NOT NULL,
                sonar_token  TEXT,          -- optional override; falls back to env SONAR_TOKEN
                channel_id   TEXT,          -- Slack channel ID to post reports to
                schedule     TEXT DEFAULT 'weekly',  -- weekly | biweekly | monthly
                added_by     TEXT,
                created_at   TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS file_paths (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                project_key  TEXT NOT NULL REFERENCES repos(project_key) ON DELETE CASCADE,
                path         TEXT NOT NULL,
                UNIQUE(project_key, path)
            );
        """)


# ── Repo CRUD ──────────────────────────────────────────────────────────────────

def add_repo(project_key: str, org_slug: str, added_by: str = None,
             sonar_token: str = None) -> bool:
    """Returns True if inserted, False if already exists."""
    with get_conn() as conn:
        existing = conn.execute(
            "SELECT project_key FROM repos WHERE project_key = ?", (project_key,)
        ).fetchone()
        if existing:
            return False
        conn.execute(
            "INSERT INTO repos (project_key, org_slug, sonar_token, added_by) VALUES (?, ?, ?, ?)",
            (project_key, org_slug, sonar_token, added_by),
        )
        return True


def remove_repo(project_key: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM repos WHERE project_key = ?", (project_key,))
        return cur.rowcount > 0


def get_repo(project_key: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM repos WHERE project_key = ?", (project_key,)
        ).fetchone()
        return dict(row) if row else None


def list_repos() -> list[dict]:
    with get_conn() as conn:
        rows = conn.execute("SELECT * FROM repos ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]


def set_channel(project_key: str, channel_id: str) -> bool:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE repos SET channel_id = ? WHERE project_key = ?",
            (channel_id, project_key),
        )
        return cur.rowcount > 0


def set_schedule(project_key: str, schedule: str) -> bool:
    assert schedule in ("weekly", "biweekly", "monthly"), "Invalid schedule"
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE repos SET schedule = ? WHERE project_key = ?",
            (schedule, project_key),
        )
        return cur.rowcount > 0


# ── File path CRUD ─────────────────────────────────────────────────────────────

def add_file_paths(project_key: str, paths: list[str]) -> int:
    """Returns count of newly added paths (skips duplicates)."""
    added = 0
    with get_conn() as conn:
        for path in paths:
            path = path.strip()
            if not path:
                continue
            try:
                conn.execute(
                    "INSERT INTO file_paths (project_key, path) VALUES (?, ?)",
                    (project_key, path),
                )
                added += 1
            except sqlite3.IntegrityError:
                pass  # duplicate — skip
    return added


def remove_file_paths(project_key: str, paths: list[str]) -> int:
    removed = 0
    with get_conn() as conn:
        for path in paths:
            cur = conn.execute(
                "DELETE FROM file_paths WHERE project_key = ? AND path = ?",
                (project_key, path.strip()),
            )
            removed += cur.rowcount
    return removed


def get_file_paths(project_key: str) -> list[str]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT path FROM file_paths WHERE project_key = ? ORDER BY path",
            (project_key,),
        ).fetchall()
        return [r["path"] for r in rows]
