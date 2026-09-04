"""Retrieve stored session context and answer questions over it."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta

from ..models import now
from . import vectors

CANDIDATES = 40
HALF_LIFE_DAYS = 21.0

PROMPT = (
    "Answer the developer's question about their own past coding sessions using only\n"
    "the context below. Each context block is labelled with a source id.\n"
    "Rules:\n"
    "- Cite the session ids you relied on, like [S1].\n"
    "- If the context does not contain the answer, say so plainly. Do not guess.\n"
    "- Be specific and brief: a few sentences, or a short list.\n"
)


def _age_penalty(last_active: str | None) -> float:
    """Gently favour recent context without letting age dominate similarity."""
    if not last_active:
        return 0.15
    try:
        when = datetime.fromisoformat(last_active)
    except ValueError:
        return 0.15
    days = max(0.0, (now() - when).total_seconds() / 86400.0)
    return 0.25 * (1.0 - 0.5 ** (days / HALF_LIFE_DAYS))


def search(conn: sqlite3.Connection, query_vector: list[float], *, k: int = 8,
           project: str | None = None, source: str | None = None,
           since_days: int | None = None, candidates: int = CANDIDATES) -> list[dict]:
    """Vector search, then rerank by recency and dedupe by session."""
    hits = vectors.knn(conn, query_vector, candidates)
    if not hits:
        return []

    by_id = {chunk_id: distance for chunk_id, distance in hits}
    marks = ",".join("?" * len(by_id))
    rows = conn.execute(
        f"""
        SELECT c.chunk_id, c.session_id, c.kind, c.text, c.source, c.project_path,
               c.session_last_active, s.title, s.branch, s.pinned
        FROM chunks c LEFT JOIN sessions s ON s.session_id = c.session_id
        WHERE c.chunk_id IN ({marks})
        """,
        list(by_id),
    ).fetchall()

    cutoff = (now() - timedelta(days=since_days)).isoformat() if since_days else None

    scored = []
    for row in rows:
        if project and project.lower() not in (row["project_path"] or "").lower():
            continue
        if source and row["source"] != source:
            continue
        if cutoff and (row["session_last_active"] or "") < cutoff:
            continue
        # sqlite-vec returns L2 distance: smaller is closer.
        similarity = 1.0 / (1.0 + by_id[row["chunk_id"]])
        scored.append(
            {
                "chunk_id": row["chunk_id"],
                "session_id": row["session_id"],
                "kind": row["kind"],
                "text": row["text"],
                "source": row["source"],
                "project_path": row["project_path"],
                "title": row["title"],
                "last_active": row["session_last_active"],
                "distance": by_id[row["chunk_id"]],
                "score": similarity - _age_penalty(row["session_last_active"]),
            }
        )

    scored.sort(key=lambda h: h["score"], reverse=True)

    # One chatty session must not crowd out everything else.
    seen: dict[str, int] = {}
    picked = []
    for hit in scored:
        count = seen.get(hit["session_id"], 0)
        if count >= 2:
            continue
        seen[hit["session_id"]] = count + 1
        picked.append(hit)
        if len(picked) >= k:
            break
    return picked


def build_context(hits: list[dict]) -> tuple[str, dict[str, dict]]:
    """Render hits as labelled blocks, returning the label -> session map."""
    labels: dict[str, dict] = {}
    blocks = []
    order: dict[str, str] = {}
    for hit in hits:
        label = order.get(hit["session_id"])
        if label is None:
            label = f"S{len(order) + 1}"
            order[hit["session_id"]] = label
            labels[label] = hit
        project = hit["project_path"] or "unknown project"
        blocks.append(
            f"[{label}] project={project} tool={hit['source']} "
            f"last_active={(hit['last_active'] or '')[:10]} kind={hit['kind']}\n"
            f"{hit['text']}"
        )
    return "\n\n".join(blocks), labels


def ask(conn: sqlite3.Connection, question: str, client, **kwargs) -> dict:
    vector = client.embed([question])[0]
    hits = search(conn, vector, **kwargs)
    if not hits:
        return {"answer": None, "hits": [], "labels": {}}
    context, labels = build_context(hits)
    prompt = f"{PROMPT}\nCONTEXT:\n{context}\n\nQUESTION: {question}\n"
    answer = client.generate(prompt, json_mode=False, num_predict=500).strip()
    return {"answer": answer, "hits": hits, "labels": labels}
