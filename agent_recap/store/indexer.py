"""Persist sessions and (re)embed their chunks.

The fingerprint check is what makes repeated runs cheap: a session whose
content has not changed keeps its existing chunks and vectors, so a warm run
issues no embed calls at all.
"""

from __future__ import annotations

import sqlite3

from ..models import Session, now
from . import chunker, vectors

EMBED_BATCH = 32


def _chunker_changed(conn: sqlite3.Connection) -> bool:
    row = conn.execute(
        "SELECT value FROM meta WHERE key = 'chunker_version'"
    ).fetchone()
    return (row["value"] if row else None) != str(chunker.CHUNKER_VERSION)


def _record_chunker_version(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO meta(key, value) VALUES ('chunker_version', ?)",
        (str(chunker.CHUNKER_VERSION),),
    )


def _needs_reindex(conn: sqlite3.Connection, session: Session, force: bool) -> bool:
    if force:
        return True
    row = conn.execute(
        "SELECT fingerprint FROM sessions WHERE session_id = ?", (session.session_id,)
    ).fetchone()
    if not row or row["fingerprint"] != session.fingerprint():
        return True
    # Metadata is current but the vectors may have been pruned away.
    count = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE session_id = ?", (session.session_id,)
    ).fetchone()[0]
    return count == 0


def _save_session(conn: sqlite3.Connection, session: Session) -> None:
    stamp = now().isoformat()
    conn.execute(
        """
        INSERT INTO sessions(session_id, source, project_path, title, started_at,
                             last_active_at, branch, model, message_count,
                             ended_mid_task, fingerprint, first_seen, last_indexed)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(session_id) DO UPDATE SET
            source=excluded.source, project_path=excluded.project_path,
            title=excluded.title, started_at=excluded.started_at,
            last_active_at=excluded.last_active_at, branch=excluded.branch,
            model=excluded.model, message_count=excluded.message_count,
            ended_mid_task=excluded.ended_mid_task,
            fingerprint=excluded.fingerprint, last_indexed=excluded.last_indexed
        """,
        (
            session.session_id, session.source, session.project_path, session.title,
            session.started_at.isoformat() if session.started_at else None,
            session.last_active_at.isoformat(), session.branch, session.model,
            session.message_count, int(session.ended_mid_task),
            session.fingerprint(), stamp, stamp,
        ),
    )
    conn.execute("DELETE FROM todos WHERE session_id = ?", (session.session_id,))
    conn.executemany(
        "INSERT INTO todos(session_id, idx, text, status, origin, verified) "
        "VALUES (?,?,?,?,?,?)",
        [
            (session.session_id, i, t.text, t.status, t.origin, int(t.verified))
            for i, t in enumerate(session.todos)
        ],
    )


def run(conn: sqlite3.Connection, sessions: list[Session], client, *,
        force: bool = False, verbose: bool = False) -> dict:
    """Index `sessions`, embedding only what changed."""
    force = force or _chunker_changed(conn)
    stale = [s for s in sessions if _needs_reindex(conn, s, force)]

    for session in sessions:
        _save_session(conn, session)
    conn.commit()

    if not stale:
        if verbose:
            print(f"  index: {len(sessions)} sessions unchanged, nothing to embed")
        return {"sessions": len(sessions), "reindexed": 0, "chunks": 0}

    pending: list[tuple[int, str]] = []
    for session in stale:
        old = [
            r["chunk_id"]
            for r in conn.execute(
                "SELECT chunk_id FROM chunks WHERE session_id = ?", (session.session_id,)
            ).fetchall()
        ]
        vectors.delete(conn, old)
        conn.execute("DELETE FROM chunks WHERE session_id = ?", (session.session_id,))

        for chunk in chunker.for_session(session):
            cur = conn.execute(
                """
                INSERT INTO chunks(session_id, source, project_path, kind, text,
                                   created_at, session_last_active, token_est)
                VALUES (?,?,?,?,?,?,?,?)
                """,
                (
                    chunk.session_id, chunk.source, chunk.project_path, chunk.kind,
                    chunk.text, now().isoformat(),
                    session.last_active_at.isoformat(), chunk.token_est,
                ),
            )
            pending.append((cur.lastrowid, chunk.text))
    conn.commit()

    for start in range(0, len(pending), EMBED_BATCH):
        batch = pending[start : start + EMBED_BATCH]
        embeddings = client.embed([text for _, text in batch])
        for (chunk_id, _), vector in zip(batch, embeddings):
            vectors.upsert(conn, chunk_id, vector)
        conn.commit()
        if verbose:
            print(f"  embedded {min(start + EMBED_BATCH, len(pending))}/{len(pending)} chunks")

    _record_chunker_version(conn)
    conn.commit()
    return {"sessions": len(sessions), "reindexed": len(stale), "chunks": len(pending)}
