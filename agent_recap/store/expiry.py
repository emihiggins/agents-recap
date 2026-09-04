"""Tiered expiry of stored context.

Age alone is a poor signal: a project paused for months can still be the one
with unfinished work. So anything pinned or still carrying an open todo is kept
regardless of age, while context whose project directory has disappeared is
dropped immediately no matter how recent.

Only *tool-reported* open todos grant immortality. Steps inferred from a plan
document do not: almost every plan has some step nobody will ever do, so
honouring those would pin the whole store forever.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

from ..models import now
from . import vectors

KEEP_PINNED = "pinned"
KEEP_OPEN_TODO = "open todo"
KEEP_RECENT = "recent"
DROP_MISSING = "project directory gone"
DROP_STALE = "older than retention window"


def classify(conn: sqlite3.Connection, max_age_days: int) -> dict[str, list[tuple[str, str]]]:
    """Return {'keep': [(session_id, reason)], 'drop': [...]}."""
    cutoff = (now() - timedelta(days=max_age_days)).isoformat()
    keep: list[tuple[str, str]] = []
    drop: list[tuple[str, str]] = []

    rows = conn.execute(
        """
        SELECT s.session_id, s.project_path, s.last_active_at, s.pinned,
               (SELECT COUNT(*) FROM todos t
                 WHERE t.session_id = s.session_id
                   AND t.status IN ('pending', 'in_progress')
                   AND t.origin = 'tool') AS open_todos
        FROM sessions s
        """
    ).fetchall()

    for row in rows:
        if row["pinned"]:
            keep.append((row["session_id"], KEEP_PINNED))
            continue
        path = row["project_path"]
        if path and not Path(path).is_dir():
            drop.append((row["session_id"], DROP_MISSING))
            continue
        if row["open_todos"]:
            keep.append((row["session_id"], KEEP_OPEN_TODO))
            continue
        if (row["last_active_at"] or "") >= cutoff:
            keep.append((row["session_id"], KEEP_RECENT))
            continue
        drop.append((row["session_id"], DROP_STALE))

    return {"keep": keep, "drop": drop}


def forget(conn: sqlite3.Connection, session_ids: list[str]) -> int:
    """Remove sessions and everything derived from them."""
    if not session_ids:
        return 0
    marks = ",".join("?" * len(session_ids))
    chunk_ids = [
        r["chunk_id"]
        for r in conn.execute(
            f"SELECT chunk_id FROM chunks WHERE session_id IN ({marks})", session_ids
        ).fetchall()
    ]
    # vec0 is a virtual table, so cascade has to be explicit or vectors orphan.
    vectors.delete(conn, chunk_ids)
    for table in ("chunks", "todos", "recaps", "sessions"):
        conn.execute(f"DELETE FROM {table} WHERE session_id IN ({marks})", session_ids)
    conn.commit()
    return len(session_ids)


def prune(conn: sqlite3.Connection, max_age_days: int, *, dry_run: bool = False) -> dict:
    verdict = classify(conn, max_age_days)
    if not dry_run:
        forget(conn, [sid for sid, _ in verdict["drop"]])
    return verdict


def set_pinned(conn: sqlite3.Connection, session_id: str, pinned: bool) -> bool:
    cur = conn.execute(
        "UPDATE sessions SET pinned = ? WHERE session_id = ?",
        (1 if pinned else 0, session_id),
    )
    conn.commit()
    return cur.rowcount > 0
