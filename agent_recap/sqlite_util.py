"""Read-only access to SQLite databases owned by *running* applications.

Cursor and VS Code hold their chat state in SQLite files that are open and
being written while we read. Rules that matter:

* Never plain-connect: SQLite would try to take a write lock / recover a hot
  journal in another app's database.
* Never use `immutable=1` on a database with a live WAL (Cursor's global store
  has one) -- it silently ignores the WAL and returns stale data.
* If read-only opening fails, copy the `.vscdb` together with its `-wal` and
  `-shm` sidecars, then read the copy. Copying the main file alone loses
  everything still in the WAL, which for per-workspace databases is nearly all
  of the content.
"""

from __future__ import annotations

import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote


@contextmanager
def open_ro(path: str | Path):
    """Yield a read-only connection to `path`, copying it if necessary."""
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(path)

    uri = f"file:{quote(str(path))}?mode=ro"
    conn = None
    tmpdir = None
    try:
        try:
            conn = sqlite3.connect(uri, uri=True, timeout=5.0)
            # Force a real read so a lock/corruption problem surfaces here,
            # where we can still fall back, rather than mid-query.
            conn.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        except sqlite3.Error:
            if conn is not None:
                conn.close()
            tmpdir = tempfile.mkdtemp(prefix="agent-recap-")
            target = Path(tmpdir) / path.name
            shutil.copy2(path, target)
            for suffix in ("-wal", "-shm"):
                sidecar = path.with_name(path.name + suffix)
                if sidecar.exists():
                    shutil.copy2(sidecar, target.with_name(target.name + suffix))
            conn = sqlite3.connect(str(target), timeout=5.0)
        conn.row_factory = sqlite3.Row
        yield conn
    finally:
        if conn is not None:
            conn.close()
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def item_table_json(conn: sqlite3.Connection, key: str):
    """Fetch and JSON-decode one `ItemTable` value, or None."""
    import json

    try:
        row = conn.execute("SELECT value FROM ItemTable WHERE key = ?", (key,)).fetchone()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except (json.JSONDecodeError, TypeError):
        return None
