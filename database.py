"""
database.py — SQLite config store.

Schema:
  repos           — global repo registry (project key + org)
  channel_configs — one row per (repo × channel); owns schedule
  file_paths      — path filters scoped to a channel_config
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
    conn.execute("PRAGMA foreign_keys=ON")
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
                sonar_token  TEXT,
                added_by     TEXT,
                created_at   TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS channel_configs (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                project_key  TEXT NOT NULL REFERENCES repos(project_key) ON DELETE CASCADE,
                channel_id   TEXT NOT NULL,
                schedule     TEXT NOT NULL DEFAULT 'weekly',
                created_at   TEXT DEFAULT (datetime('now')),
                UNIQUE(project_key, channel_id)
            );

            CREATE TABLE IF NOT EXISTS file_paths (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                config_id    INTEGER NOT NULL REFERENCES channel_configs(id) ON DELETE CASCADE,
                path         TEXT NOT NULL,
                UNIQUE(config_id, path)
            );
        """)


# ── Repos ──────────────────────────────────────────────────────────────────────

def add_repo(project_key: str, org_slug: str, added_by: str = None,
             sonar_token: str = None) -> bool:
    with get_conn() as conn:
        if conn.execute("SELECT 1 FROM repos WHERE project_key=?", (project_key,)).fetchone():
            return False
        conn.execute(
            "INSERT INTO repos (project_key, org_slug, sonar_token, added_by) VALUES (?,?,?,?)",
            (project_key, org_slug, sonar_token, added_by),
        )
        return True


def remove_repo(project_key: str) -> bool:
    with get_conn() as conn:
        return conn.execute("DELETE FROM repos WHERE project_key=?", (project_key,)).rowcount > 0


def get_repo(project_key: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM repos WHERE project_key=?", (project_key,)).fetchone()
        return dict(row) if row else None


def list_all_repos() -> list[dict]:
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM repos ORDER BY project_key")]


# ── Channel configs ────────────────────────────────────────────────────────────

def track_repo_in_channel(project_key: str, channel_id: str) -> bool:
    """Returns True if newly created, False if already tracked."""
    with get_conn() as conn:
        if conn.execute(
            "SELECT 1 FROM channel_configs WHERE project_key=? AND channel_id=?",
            (project_key, channel_id)
        ).fetchone():
            return False
        conn.execute(
            "INSERT INTO channel_configs (project_key, channel_id) VALUES (?,?)",
            (project_key, channel_id),
        )
        return True


def untrack_repo_in_channel(project_key: str, channel_id: str) -> bool:
    with get_conn() as conn:
        return conn.execute(
            "DELETE FROM channel_configs WHERE project_key=? AND channel_id=?",
            (project_key, channel_id),
        ).rowcount > 0


def get_channel_config(project_key: str, channel_id: str) -> Optional[dict]:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM channel_configs WHERE project_key=? AND channel_id=?",
            (project_key, channel_id),
        ).fetchone()
        return dict(row) if row else None


def list_repos_in_channel(channel_id: str) -> list[dict]:
    """All repos tracked in a channel, joined with repo info."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT cc.id, cc.project_key, cc.channel_id, cc.schedule,
                   r.org_slug, r.sonar_token
            FROM channel_configs cc
            JOIN repos r ON r.project_key = cc.project_key
            WHERE cc.channel_id = ?
            ORDER BY cc.project_key
        """, (channel_id,)).fetchall()
        return [dict(r) for r in rows]


def list_all_channel_configs() -> list[dict]:
    """All (repo, channel) pairs — used by scheduler on startup."""
    with get_conn() as conn:
        rows = conn.execute("""
            SELECT cc.id, cc.project_key, cc.channel_id, cc.schedule,
                   r.org_slug, r.sonar_token
            FROM channel_configs cc
            JOIN repos r ON r.project_key = cc.project_key
        """).fetchall()
        return [dict(r) for r in rows]


def set_schedule(project_key: str, channel_id: str, schedule: str) -> bool:
    with get_conn() as conn:
        return conn.execute(
            "UPDATE channel_configs SET schedule=? WHERE project_key=? AND channel_id=?",
            (schedule, project_key, channel_id),
        ).rowcount > 0


# ── File paths (scoped to channel_config) ─────────────────────────────────────

def _config_id(conn, project_key: str, channel_id: str) -> Optional[int]:
    row = conn.execute(
        "SELECT id FROM channel_configs WHERE project_key=? AND channel_id=?",
        (project_key, channel_id),
    ).fetchone()
    return row["id"] if row else None


def add_file_paths(project_key: str, channel_id: str, paths: list[str]) -> int:
    added = 0
    with get_conn() as conn:
        cid = _config_id(conn, project_key, channel_id)
        if cid is None:
            return 0
        for path in paths:
            path = path.strip()
            if not path:
                continue
            try:
                conn.execute("INSERT INTO file_paths (config_id, path) VALUES (?,?)", (cid, path))
                added += 1
            except sqlite3.IntegrityError:
                pass
    return added


def remove_file_paths(project_key: str, channel_id: str, paths: list[str]) -> int:
    removed = 0
    with get_conn() as conn:
        cid = _config_id(conn, project_key, channel_id)
        if cid is None:
            return 0
        for path in paths:
            removed += conn.execute(
                "DELETE FROM file_paths WHERE config_id=? AND path=?",
                (cid, path.strip()),
            ).rowcount
    return removed


def get_file_paths(project_key: str, channel_id: str) -> list[str]:
    with get_conn() as conn:
        cid = _config_id(conn, project_key, channel_id)
        if cid is None:
            return []
        rows = conn.execute(
            "SELECT path FROM file_paths WHERE config_id=? ORDER BY path", (cid,)
        ).fetchall()
        return [r["path"] for r in rows]
