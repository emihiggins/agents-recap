"""Persistent store: session metadata, cached recaps, and chunk vectors."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import sqlite_vec

from .. import config

SCHEMA_VERSION = 2

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id     TEXT PRIMARY KEY,
    source         TEXT NOT NULL,
    project_path   TEXT,
    title          TEXT,
    started_at     TEXT,
    last_active_at TEXT NOT NULL,
    branch         TEXT,
    model          TEXT,
    message_count  INTEGER,
    ended_mid_task INTEGER DEFAULT 0,
    fingerprint    TEXT,
    first_seen     TEXT,
    last_indexed   TEXT,
    pinned         INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS todos (
    session_id TEXT NOT NULL,
    idx        INTEGER NOT NULL,
    text       TEXT NOT NULL,
    status     TEXT NOT NULL,
    origin     TEXT NOT NULL DEFAULT 'tool',
    verified   INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (session_id, idx)
);

CREATE TABLE IF NOT EXISTS recaps (
    session_id   TEXT PRIMARY KEY,
    recap        TEXT,
    next_step    TEXT,
    model        TEXT,
    generated_at TEXT,
    fingerprint  TEXT
);

CREATE TABLE IF NOT EXISTS chunks (
    chunk_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT NOT NULL,
    source              TEXT,
    project_path        TEXT,
    kind                TEXT,
    text                TEXT NOT NULL,
    created_at          TEXT,
    session_last_active TEXT,
    token_est           INTEGER
);

CREATE INDEX IF NOT EXISTS idx_chunks_session ON chunks(session_id);
CREATE INDEX IF NOT EXISTS idx_sessions_active ON sessions(last_active_at);
CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0(
    chunk_id INTEGER PRIMARY KEY,
    embedding float[{config.EMBED_DIM}]
);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    """Open (creating if needed) the store with sqlite-vec loaded."""
    if path is None:
        config.ensure_data_dir()
        path = config.load().db_path
    path = Path(path)
    if path.parent != Path("."):
        path.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    _migrate(conn)
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)",
        (str(SCHEMA_VERSION),),
    )
    conn.commit()
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a store was first created."""
    existing = {r[1] for r in conn.execute("PRAGMA table_info(todos)").fetchall()}
    if "origin" not in existing:
        conn.execute("ALTER TABLE todos ADD COLUMN origin TEXT NOT NULL DEFAULT 'tool'")
    if "verified" not in existing:
        conn.execute("ALTER TABLE todos ADD COLUMN verified INTEGER NOT NULL DEFAULT 1")


def stats(conn: sqlite3.Connection) -> dict:
    def one(sql: str) -> int:
        try:
            return conn.execute(sql).fetchone()[0] or 0
        except sqlite3.Error:
            return 0

    return {
        "sessions": one("SELECT COUNT(*) FROM sessions"),
        "chunks": one("SELECT COUNT(*) FROM chunks"),
        "vectors": one("SELECT COUNT(*) FROM vec_chunks"),
        "recaps": one("SELECT COUNT(*) FROM recaps"),
        "pinned": one("SELECT COUNT(*) FROM sessions WHERE pinned = 1"),
        "oldest": (conn.execute("SELECT MIN(last_active_at) FROM sessions").fetchone()[0] or "-"),
    }
