"""Cursor composer/agent sessions.

Everything lives in Cursor's *global* state.vscdb. The `composerHeaders` table
is the cheap driver: ~450 small rows carrying title, subtitle, timestamps and
the project path, so we never touch the ~1.6 GB of message bubbles and opaque
`agentKv:blob` rows. `conversation-search.db` is deliberately ignored: it is a
stale FTS index whose body text has no role markers.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta

from .. import config
from ..models import Session, Todo, now, utc
from ..sqlite_util import open_ro

_STATUS = {
    "pending": "pending",
    "not-started": "pending",
    "in_progress": "in_progress",
    "in-progress": "in_progress",
    "completed": "completed",
    "cancelled": "completed",
}

GLOBAL_DB = config.CURSOR_USER / "globalStorage/state.vscdb"


def _project_path(header: dict) -> str | None:
    ident = header.get("workspaceIdentifier") or {}
    uri = ident.get("uri") or {}
    path = uri.get("fsPath")
    if path:
        return path
    # Multi-root workspaces reference an indirection file that may no longer
    # exist; there is nothing better to report than unknown.
    return None


def _last_texts(data: dict) -> tuple[str | None, str | None, int]:
    """Last user and assistant prose from the pre-summarized message stubs.

    `fullConversationHeadersOnly` already carries a text preview per message,
    which avoids reading any `bubbleId:` row. Tool calls are also type 2 but
    carry no text, so `grouping.hasText` is the filter that matters.
    """
    headers = data.get("fullConversationHeadersOnly") or []
    last_user = last_assistant = None
    count = 0
    for stub in headers:
        if not isinstance(stub, dict):
            continue
        grouping = stub.get("grouping") or {}
        if not grouping.get("hasText"):
            continue
        preview = (grouping.get("textPreview") or "").strip()
        if not preview:
            continue
        count += 1
        if stub.get("type") == 1:
            last_user = preview
        elif stub.get("type") == 2:
            last_assistant = preview
    return last_user, last_assistant, count


def _todos(data: dict) -> list[Todo]:
    out = []
    for item in data.get("todos") or []:
        if not isinstance(item, dict):
            continue
        text = (item.get("content") or item.get("title") or "").strip()
        if not text:
            continue
        out.append(Todo(text=text, status=_STATUS.get(item.get("status") or "", "pending")))
    return out


def collect(window_days: int = 7, *, max_sessions: int = 40) -> list[Session]:
    if not GLOBAL_DB.exists():
        return []

    cutoff = now() - timedelta(days=window_days)
    sessions: list[Session] = []

    with open_ro(GLOBAL_DB) as conn:
        try:
            rows = conn.execute(
                """
                SELECT composerId, lastUpdatedAt, value
                FROM composerHeaders
                WHERE isArchived = 0 AND isSubagent = 0
                ORDER BY recency DESC
                LIMIT ?
                """,
                (max_sessions,),
            ).fetchall()
        except sqlite3.Error:
            return []

        for row in rows:
            try:
                header = json.loads(row["value"] or "{}")
            except (json.JSONDecodeError, TypeError):
                continue

            last_active = utc(header.get("lastUpdatedAt") or row["lastUpdatedAt"])
            if not last_active or last_active < cutoff:
                continue

            project = _project_path(header)
            if config.is_noise(project):
                continue

            composer_id = row["composerId"]
            detail = {}
            try:
                drow = conn.execute(
                    "SELECT value FROM cursorDiskKV WHERE key = ?",
                    (f"composerData:{composer_id}",),
                ).fetchone()
                if drow and drow["value"]:
                    detail = json.loads(drow["value"])
            except (sqlite3.Error, json.JSONDecodeError, TypeError):
                detail = {}

            last_user, last_assistant, msg_count = _last_texts(detail)
            todos = _todos(detail) or _todos(header)

            title = (header.get("name") or "").strip() or None
            subtitle = (header.get("subtitle") or "").strip() or None

            sessions.append(
                Session(
                    source="cursor",
                    session_id=composer_id,
                    last_active_at=last_active,
                    title=title,
                    project_path=project,
                    started_at=utc(header.get("createdAt")),
                    model=((detail.get("modelConfig") or {}).get("modelName")
                           or header.get("unifiedMode")),
                    message_count=msg_count or None,
                    last_user_text=last_user,
                    last_assistant_text=last_assistant,
                    todos=todos,
                    ended_mid_task=bool(header.get("hasPendingPlan"))
                    or any(t.is_open for t in todos),
                    extras={
                        "subtitle": subtitle,
                        "mode": header.get("unifiedMode"),
                        "context_pct": header.get("contextUsagePercent"),
                        "files_changed": header.get("filesChangedCount"),
                        "lines_added": header.get("totalLinesAdded"),
                        "lines_removed": header.get("totalLinesRemoved"),
                    },
                )
            )
    return sessions


def probe() -> dict:
    """Check Cursor's global store still has the tables and keys we read."""
    if not GLOBAL_DB.exists():
        return {"name": "cursor", "present": False, "healthy": True,
                "detail": "not installed"}

    missing = []
    detail = ""
    try:
        with open_ro(GLOBAL_DB) as conn:
            tables = {
                r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            if "composerHeaders" not in tables:
                missing.append("composerHeaders table gone (Cursor changed its schema)")
            else:
                columns = {
                    r[1] for r in conn.execute("PRAGMA table_info(composerHeaders)").fetchall()
                }
                for needed in ("composerId", "recency", "isArchived", "isSubagent", "value"):
                    if needed not in columns:
                        missing.append(f"composerHeaders.{needed} missing")
                count = conn.execute("SELECT COUNT(*) FROM composerHeaders").fetchone()[0]
                detail = f"{count} composer sessions"
                # Sample a window rather than the newest row: unopened draft
                # composers legitimately carry no workspace, so requiring a
                # path on any single header gives false alarms.
                rows = conn.execute(
                    "SELECT value FROM composerHeaders ORDER BY recency DESC LIMIT 20"
                ).fetchall()
                sampled = [json.loads(r[0] or "{}") for r in rows]
                if sampled and not any(_project_path(h) for h in sampled):
                    missing.append(
                        "no workspaceIdentifier.uri.fsPath in the 20 most recent "
                        "headers (Cursor may have moved the project path)"
                    )
                if sampled:
                    with_path = sum(1 for h in sampled if _project_path(h))
                    detail += f", {with_path}/{len(sampled)} recent with a project path"
            if "cursorDiskKV" not in tables:
                missing.append("cursorDiskKV table gone")
    except (sqlite3.Error, json.JSONDecodeError, OSError) as exc:
        return {"name": "cursor", "present": True, "healthy": False,
                "detail": f"cannot read store: {exc}"}

    return {"name": "cursor", "present": True, "healthy": not missing,
            "detail": "; ".join(missing) if missing else detail}
