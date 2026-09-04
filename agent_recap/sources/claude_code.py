"""Claude Code sessions from ~/.claude.

Transcripts total ~200 MB across ~300 files here, with individual files up to
15 MB, so nothing full-parses a transcript: we read a small head for identity
and seek the tail for current state.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config, plans
from ..models import GitState, Session, Todo, now, utc

HEAD_BYTES = 64 * 1024
TAIL_BYTES = 1024 * 1024

# Claude Code's task tool statuses map straight onto ours.
_TASK_STATUS = {
    "pending": "pending",
    "in_progress": "in_progress",
    "completed": "completed",
    "cancelled": "completed",
}


def _iter_json_lines(raw: bytes, *, drop_first_partial: bool):
    lines = raw.split(b"\n")
    if drop_first_partial and lines:
        lines = lines[1:]  # a tail seek almost always lands mid-line
    for line in lines:
        line = line.strip()
        if not line.startswith(b"{"):
            continue
        try:
            yield json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue  # truncated tail line


# Harness-generated user turns: notifications, reminders and slash-command
# echoes. They are not things the developer said, so they must not become the
# "last message from you" or feed the summarizer.
_SYNTHETIC = (
    "<task-notification",
    "<system-reminder",
    "<local-command-",
    "<command-name",
    "<command-message",
    "<user-prompt-submit-hook",
    "[Request interrupted",
    "Caveat: The messages below",
)


def _is_synthetic(text: str) -> bool:
    stripped = text.lstrip()
    if stripped.startswith(_SYNTHETIC):
        return True
    # Also catch turns that are mostly a notification with a little text around it.
    return len(text) > 0 and sum(
        len(marker) for marker in _SYNTHETIC if marker in text
    ) > 0 and "<task-notification" in text


def _text_blocks(message: dict) -> str:
    """Join the human-readable text of an API message, skipping tools/thinking."""
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "\n".join(p for p in parts if p).strip()


def _parse_ts(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _live_sessions() -> dict[str, dict]:
    """Map sessionId -> live record, from ~/.claude/sessions/<pid>.json.

    Stale files linger after a hard exit, so every pid is verified. The sibling
    `<pid>.<hex>.key` files are IPC secrets and are never read.
    """
    out: dict[str, dict] = {}
    directory = config.CLAUDE_DIR / "sessions"
    if not directory.is_dir():
        return out
    for path in directory.glob("*.json"):
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        pid, session_id = data.get("pid"), data.get("sessionId")
        if not pid or not session_id:
            continue
        try:
            os.kill(int(pid), 0)
        except (OSError, ValueError):
            continue  # process is gone; record is stale
        out[session_id] = data
    return out


def _transcripts(window_days: int) -> list[Path]:
    root = config.CLAUDE_DIR / "projects"
    if not root.is_dir():
        return []
    cutoff = time.time() - window_days * 86400
    found = []
    for path in root.glob("*/*.jsonl"):
        # `<session>/subagents/agent-*.jsonl` are sidechains, not user sessions.
        if "subagents" in path.parts:
            continue
        try:
            if path.stat().st_mtime >= cutoff:
                found.append(path)
        except OSError:
            continue
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def _read_head(path: Path) -> tuple[dict, datetime | None]:
    """Identity fields and start time from the first records."""
    with path.open("rb") as fh:
        raw = fh.read(HEAD_BYTES)
    identity: dict = {}
    started = None
    for rec in _iter_json_lines(raw, drop_first_partial=False):
        for key in ("sessionId", "cwd", "gitBranch", "version"):
            if key not in identity and rec.get(key):
                identity[key] = rec[key]
        if started is None:
            started = _parse_ts(rec.get("timestamp"))
        if started is not None and {"sessionId", "cwd"} <= identity.keys():
            break
    return identity, started


def _read_tail(path: Path, size: int) -> dict:
    """Current state: titles, last messages, open tasks, mid-task signal."""
    file_size = path.stat().st_size
    offset = max(0, file_size - size)
    with path.open("rb") as fh:
        fh.seek(offset)
        raw = fh.read()

    state: dict = {
        "cwd": None, "branch": None, "model": None, "title": None,
        "last_prompt": None, "last_user": None, "last_assistant": None,
        "last_ts": None, "user_msgs": 0, "assistant_msgs": 0,
        "stop_reason": None, "slug": None,
    }
    turns: list[tuple[str, str]] = []   # recent (role, text), chronological
    files: list[str] = []               # paths the agent edited or created
    tasks: dict[str, dict] = {}
    tool_uses: set[str] = set()
    tool_results: set[str] = set()

    for rec in _iter_json_lines(raw, drop_first_partial=offset > 0):
        rtype = rec.get("type")

        # Cheap sidecar records Claude Code writes for its own UI.
        if rtype == "ai-title":
            state["title"] = rec.get("aiTitle") or state["title"]
            continue
        if rtype == "last-prompt":
            state["last_prompt"] = rec.get("lastPrompt") or state["last_prompt"]
            continue
        if rtype not in ("user", "assistant"):
            continue
        if rec.get("isSidechain"):
            continue

        state["cwd"] = rec.get("cwd") or state["cwd"]
        state["branch"] = rec.get("gitBranch") or state["branch"]
        ts = _parse_ts(rec.get("timestamp"))
        if ts and (state["last_ts"] is None or ts > state["last_ts"]):
            state["last_ts"] = ts

        message = rec.get("message") or {}

        if rtype == "user":
            if rec.get("isMeta"):
                continue
            content = message.get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tid = block.get("tool_use_id")
                        if tid:
                            tool_results.add(tid)
            text = _text_blocks(message)
            if text and not _is_synthetic(text):
                state["user_msgs"] += 1
                state["last_user"] = text
                turns.append(("you", text))
            continue

        # assistant
        # `slug` names the plan-mode document in ~/.claude/plans/<slug>.md
        state["slug"] = rec.get("slug") or state["slug"]
        state["model"] = message.get("model") or state["model"]
        state["stop_reason"] = message.get("stop_reason") or state["stop_reason"]
        text = _text_blocks(message)
        if text:
            state["assistant_msgs"] += 1
            state["last_assistant"] = text
            turns.append(("agent", text))
        for block in message.get("content") or []:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("id"):
                tool_uses.add(block["id"])
            name = block.get("name")
            payload = block.get("input") or {}
            if name in ("Edit", "Write", "NotebookEdit", "MultiEdit"):
                target = payload.get("file_path") or payload.get("notebook_path")
                if target and target not in files:
                    files.append(target)
            if name == "TaskCreate":
                # ids are session-scoped ordinals assigned in creation order
                tid = str(len(tasks) + 1)
                tasks[tid] = {
                    "text": payload.get("subject") or payload.get("description") or f"task {tid}",
                    "status": "pending",
                }
            elif name == "TaskUpdate":
                tid = str(payload.get("taskId") or "")
                status = _TASK_STATUS.get(payload.get("status") or "", "pending")
                if tid in tasks:
                    tasks[tid]["status"] = status
                elif tid:
                    # TaskCreate scrolled out of the tail window; keep the
                    # status, which is the part that decides "still open".
                    tasks[tid] = {"text": f"task #{tid}", "status": status}

    state["tasks"] = [Todo(text=t["text"], status=t["status"]) for t in tasks.values()]
    state["pending_tools"] = bool(tool_uses - tool_results)
    state["turns"] = turns[-6:]
    state["files"] = files[-12:]
    return state


def collect(window_days: int = 7, *, tail_bytes: int = TAIL_BYTES) -> list[Session]:
    live = _live_sessions()
    sessions: list[Session] = []

    for path in _transcripts(window_days):
        try:
            identity, started = _read_head(path)
            state = _read_tail(path, tail_bytes)
        except OSError:
            continue

        session_id = identity.get("sessionId") or path.stem
        project = state["cwd"] or identity.get("cwd")
        if config.is_noise(project):
            continue
        if not state["user_msgs"] and not state["assistant_msgs"] and not state["last_prompt"]:
            continue  # nothing ever happened here

        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        last_active = state["last_ts"] or mtime

        record = live.get(session_id)
        live_status = live_state = waiting_for = None
        state_since = None
        if record:
            live_state = record.get("status") or "running"
            waiting_for = record.get("waitingFor")
            live_status = f"{live_state} — {waiting_for}" if waiting_for else live_state
            # statusUpdatedAt tells us how long it has been stuck like this,
            # which is what makes "blocked on you" actionable.
            state_since = utc(record.get("statusUpdatedAt") or record.get("updatedAt"))

        branch = state["branch"] or identity.get("gitBranch")
        if branch == "HEAD":
            branch = None  # detached or not a repo; git_state reports it better

        todos = list(state["tasks"])

        # Claude Code's task tools are unused in practice, so plan documents
        # are the only record of intended work. Completion is judged against
        # files on disk; see plans.assess for why this is not a model call.
        plan_steps, plan_name = plans.steps_for({"plan_slug": state["slug"]})
        for text, status, verified in plans.assess(plan_steps, project):
            todos.append(Todo(text=text, status=status, origin="plan", verified=verified))

        ended_mid_task = bool(state["pending_tools"]) or any(t.is_open for t in todos)

        sessions.append(
            Session(
                source="claude-code",
                session_id=session_id,
                last_active_at=last_active,
                title=state["title"],
                project_path=project,
                started_at=started,
                live=record is not None,
                live_status=live_status,
                live_state=live_state,
                waiting_for=waiting_for,
                state_since=state_since,
                branch=branch,
                model=state["model"],
                message_count=state["user_msgs"] + state["assistant_msgs"],
                last_user_text=state["last_user"] or state["last_prompt"],
                last_assistant_text=state["last_assistant"],
                todos=todos,
                ended_mid_task=ended_mid_task,
                plan_steps=plan_steps,
                plan_name=plan_name,
                extras={
                    "transcript": str(path),
                    "plan_slug": state["slug"],
                    "turns": state["turns"],
                    "files_touched": state["files"],
                    "counts_are_partial": path.stat().st_size > tail_bytes,
                },
            )
        )
    return sessions


def probe() -> dict:
    """Check that this store still looks the way the parser expects.

    These formats are undocumented and will change. Without a probe, a schema
    change shows up as "no sessions found", which reads like having done no
    work rather than like a broken reader.
    """
    root = config.CLAUDE_DIR / "projects"
    if not root.is_dir():
        return {"name": "claude-code", "present": False, "healthy": True,
                "detail": "not installed"}

    files = [p for p in root.glob("*/*.jsonl") if "subagents" not in p.parts]
    if not files:
        return {"name": "claude-code", "present": True, "healthy": True,
                "detail": "no transcripts yet"}

    newest = max(files, key=lambda p: p.stat().st_mtime)
    try:
        with newest.open("rb") as fh:
            raw = fh.read(HEAD_BYTES)
        records = list(_iter_json_lines(raw, drop_first_partial=False))
    except OSError as exc:
        return {"name": "claude-code", "present": True, "healthy": False,
                "detail": f"cannot read {newest.name}: {exc}"}

    missing = []
    types = {r.get("type") for r in records}
    if not types & {"user", "assistant"}:
        missing.append("no user/assistant records")
    if not any(r.get("cwd") for r in records):
        missing.append("no cwd field")
    if not any(r.get("timestamp") for r in records):
        missing.append("no timestamp field")
    if not any(r.get("sessionId") for r in records):
        missing.append("no sessionId field")

    sessions_dir = config.CLAUDE_DIR / "sessions"
    detail = f"{len(files)} transcripts"
    if sessions_dir.is_dir():
        detail += f", {len(list(sessions_dir.glob('*.json')))} live records"
    else:
        missing.append("no ~/.claude/sessions (live status unavailable)")

    return {
        "name": "claude-code",
        "present": True,
        "healthy": not missing,
        "detail": "; ".join(missing) if missing else detail,
    }
