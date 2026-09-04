"""VS Code Copilot Chat sessions.

Two on-disk formats coexist: a legacy single-object `.json` (the large
majority) and a newer append-only `.jsonl` journal that has to be replayed.
The global `chat.ChatSessionStore.index` gives titles/timestamps cheaply but
appears to cover only empty-window sessions, so workspace directories are
walked too.
"""

from __future__ import annotations

import json
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import unquote, urlparse

from .. import config
from ..models import Session, Todo, now, utc
from ..sqlite_util import item_table_json, open_ro

_STATUS = {
    "not-started": "pending",
    "pending": "pending",
    "in-progress": "in_progress",
    "in_progress": "in_progress",
    "completed": "completed",
}

GLOBAL_DB = config.VSCODE_USER / "globalStorage/state.vscdb"
WORKSPACE_STORAGE = config.VSCODE_USER / "workspaceStorage"
EMPTY_WINDOW = config.VSCODE_USER / "globalStorage/emptyWindowChatSessions"


def _set_path(root, path: list, value) -> None:
    """Apply a journal `set` at a JSON path, creating containers as needed."""
    node = root
    for key in path[:-1]:
        if isinstance(node, list):
            if not isinstance(key, int) or key >= len(node):
                return
            node = node[key]
        elif isinstance(node, dict):
            node = node.setdefault(key, {})
        else:
            return
    last = path[-1]
    if isinstance(node, list):
        if isinstance(last, int) and last < len(node):
            node[last] = value
    elif isinstance(node, dict):
        node[last] = value


def _append_path(root, path: list, value) -> None:
    node = root
    for key in path:
        if isinstance(node, list):
            if not isinstance(key, int) or key >= len(node):
                return
            node = node[key]
        elif isinstance(node, dict):
            node = node.setdefault(key, [] if key == path[-1] else {})
        else:
            return
    if isinstance(node, list):
        node.extend(value if isinstance(value, list) else [value])


def _replay_journal(path: Path) -> dict:
    """Rebuild a session document from a `.jsonl` journal."""
    doc: dict = {}
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            kind, key, value = rec.get("kind"), rec.get("k"), rec.get("v")
            if kind == 0 and isinstance(value, dict):
                doc = value
            elif kind == 1 and isinstance(key, list) and key:
                _set_path(doc, key, value)
            elif kind == 2 and isinstance(key, list) and key:
                _append_path(doc, key, value)
    return doc


def _load_session_doc(path: Path) -> dict | None:
    try:
        if path.suffix == ".jsonl":
            return _replay_journal(path)
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError):
        return None


def _part_text(part) -> str:
    """Extract prose from one heterogeneous response part.

    Plain markdown parts carry no `kind` at all, so the undefined case is the
    common one rather than an edge case.
    """
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return ""
    if part.get("kind") in (None, "markdownContent", "markdownVuln"):
        value = part.get("value")
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return value.get("value") or ""
    return ""


def _todos_from_response(parts) -> list[Todo]:
    """Copilot's todo list is a `manage_todo_list` tool invocation part."""
    found: list[Todo] = []
    for part in parts or []:
        if not isinstance(part, dict):
            continue
        if part.get("toolId") != "manage_todo_list":
            continue
        specific = part.get("toolSpecificData") or {}
        items = specific.get("todoList") or []
        current = []
        for item in items:
            if not isinstance(item, dict):
                continue
            text = (item.get("title") or item.get("description") or "").strip()
            if text:
                current.append(
                    Todo(text=text, status=_STATUS.get(item.get("status") or "", "pending"))
                )
        if current:
            found = current  # keep the last list in the session
    return found


def _workspace_paths() -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    if not WORKSPACE_STORAGE.is_dir():
        return out
    for directory in WORKSPACE_STORAGE.iterdir():
        if not directory.is_dir():
            continue
        meta = directory / "workspace.json"
        path = None
        if meta.exists():
            try:
                data = json.loads(meta.read_text())
            except (OSError, json.JSONDecodeError):
                data = {}
            folder = data.get("folder")
            if folder:
                path = unquote(urlparse(folder).path) or None
        out[directory.name] = path
    return out


def _parse_doc(doc: dict, path: Path, project: str | None) -> Session | None:
    requests = doc.get("requests") or []
    input_state = doc.get("inputState") or {}

    last_user = last_assistant = None
    todos: list[Todo] = []
    for req in requests:
        if not isinstance(req, dict):
            continue
        message = req.get("message") or {}
        text = (message.get("text") or "").strip()
        if text:
            last_user = text
        parts = req.get("response") or []
        reply = "\n".join(t for t in (_part_text(p) for p in parts) if t).strip()
        if reply:
            last_assistant = reply
        found = _todos_from_response(parts)
        if found:
            todos = found

    if not requests and not doc.get("customTitle"):
        return None

    last_active = utc(doc.get("lastMessageDate")) or utc(doc.get("creationDate"))
    if not last_active:
        try:
            last_active = utc(path.stat().st_mtime * 1000)
        except OSError:
            return None

    session_id = doc.get("sessionId") or path.stem
    model = input_state.get("selectedModel") or doc.get("selectedModel")
    if isinstance(model, dict):
        model = model.get("id") or model.get("name")

    return Session(
        source="vscode",
        session_id=str(session_id),
        last_active_at=last_active,
        title=(doc.get("customTitle") or "").strip() or None,
        project_path=project,
        started_at=utc(doc.get("creationDate")),
        model=model if isinstance(model, str) else None,
        message_count=len(requests) or None,
        last_user_text=last_user,
        last_assistant_text=last_assistant,
        todos=todos,
        ended_mid_task=bool(doc.get("hasPendingEdits")) or any(t.is_open for t in todos),
        extras={"file": str(path)},
    )


def _index_titles() -> dict[str, dict]:
    if not GLOBAL_DB.exists():
        return {}
    try:
        with open_ro(GLOBAL_DB) as conn:
            data = item_table_json(conn, "chat.ChatSessionStore.index") or {}
    except (OSError, Exception):
        return {}
    entries = data.get("entries") or {}
    return {k: v for k, v in entries.items() if isinstance(v, dict)}


def collect(window_days: int = 7) -> list[Session]:
    cutoff_ts = time.time() - window_days * 86400
    cutoff = now() - timedelta(days=window_days)
    projects = _workspace_paths()
    index = _index_titles()

    candidates: list[tuple[Path, str | None]] = []
    for hash_name, project in projects.items():
        directory = WORKSPACE_STORAGE / hash_name / "chatSessions"
        if not directory.is_dir():
            continue
        for path in list(directory.glob("*.json")) + list(directory.glob("*.jsonl")):
            try:
                if path.stat().st_mtime >= cutoff_ts:
                    candidates.append((path, project))
            except OSError:
                continue
    if EMPTY_WINDOW.is_dir():
        for path in list(EMPTY_WINDOW.glob("*.json")) + list(EMPTY_WINDOW.glob("*.jsonl")):
            try:
                if path.stat().st_mtime >= cutoff_ts:
                    candidates.append((path, None))
            except OSError:
                continue

    sessions: list[Session] = []
    for path, project in candidates:
        if config.is_noise(project):
            continue
        doc = _load_session_doc(path)
        if not doc:
            continue
        session = _parse_doc(doc, path, project)
        if not session or session.last_active_at < cutoff:
            continue
        if not session.title:
            session.title = (index.get(session.session_id, {}).get("title") or "").strip() or None
        if session.message_count is None and not session.last_user_text:
            continue  # empty shell
        sessions.append(session)
    return sessions


def probe() -> dict:
    """Check VS Code chat sessions still parse into the shape we expect."""
    if not WORKSPACE_STORAGE.is_dir():
        return {"name": "vscode", "present": False, "healthy": True,
                "detail": "not installed"}

    files = list(WORKSPACE_STORAGE.glob("*/chatSessions/*.json")) + \
        list(WORKSPACE_STORAGE.glob("*/chatSessions/*.jsonl"))
    if not files:
        return {"name": "vscode", "present": True, "healthy": True,
                "detail": "no chat sessions yet"}

    newest = max(files, key=lambda p: p.stat().st_mtime)
    doc = _load_session_doc(newest)
    if not doc:
        return {"name": "vscode", "present": True, "healthy": False,
                "detail": f"cannot parse {newest.name} ({newest.suffix} reader)"}

    missing = []
    if "requests" not in doc and "customTitle" not in doc:
        missing.append(f"{newest.suffix} sessions no longer carry requests/customTitle")
    requests = doc.get("requests") or []
    if requests and not isinstance(requests, list):
        missing.append("requests is no longer a list")
    if requests and not (requests[0].get("message") or {}).get("text"):
        missing.append("requests[].message.text missing")

    legacy = sum(1 for p in files if p.suffix == ".json")
    detail = f"{legacy} legacy + {len(files) - legacy} journal sessions"
    return {"name": "vscode", "present": True, "healthy": not missing,
            "detail": "; ".join(missing) if missing else detail}
