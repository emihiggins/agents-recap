"""sqlite-vec helpers: pack float lists and run KNN."""

from __future__ import annotations

import sqlite3
import struct

from .. import config


def pack(vector: list[float]) -> bytes:
    if len(vector) != config.EMBED_DIM:
        raise ValueError(
            f"expected {config.EMBED_DIM}-dim embedding, got {len(vector)}; "
            "the configured embed_model does not match the store schema"
        )
    return struct.pack(f"{len(vector)}f", *vector)


def upsert(conn: sqlite3.Connection, chunk_id: int, vector: list[float]) -> None:
    conn.execute("DELETE FROM vec_chunks WHERE chunk_id = ?", (chunk_id,))
    conn.execute(
        "INSERT INTO vec_chunks(chunk_id, embedding) VALUES (?, ?)",
        (chunk_id, pack(vector)),
    )


def delete(conn: sqlite3.Connection, chunk_ids: list[int]) -> None:
    if not chunk_ids:
        return
    marks = ",".join("?" * len(chunk_ids))
    conn.execute(f"DELETE FROM vec_chunks WHERE chunk_id IN ({marks})", chunk_ids)


def knn(conn: sqlite3.Connection, vector: list[float], k: int = 40) -> list[tuple[int, float]]:
    """Return [(chunk_id, distance)] nearest to `vector`, closest first."""
    rows = conn.execute(
        """
        SELECT chunk_id, distance FROM vec_chunks
        WHERE embedding MATCH ? AND k = ?
        ORDER BY distance
        """,
        (pack(vector), k),
    ).fetchall()
    return [(r["chunk_id"], r["distance"]) for r in rows]
