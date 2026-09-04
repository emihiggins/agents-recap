"""Normalized session model shared by every source."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

OPEN_STATUSES = {"pending", "in_progress"}


@dataclass
class Todo:
    text: str
    status: str  # pending | in_progress | completed
    # "tool" = the agent's own todo list, authoritative.
    # "plan" = inferred from a plan document, a guess and labelled as one.
    origin: str = "tool"
    # False when we could not confirm the status (an inferred plan step whose
    # artefacts we cannot check on disk).
    verified: bool = True

    @property
    def is_open(self) -> bool:
        return self.status in OPEN_STATUSES

    @property
    def inferred(self) -> bool:
        return self.origin == "plan"


@dataclass
class GitState:
    branch: str | None = None
    dirty_files: int = 0
    last_commit: str | None = None
    ahead: int | None = None
    note: str | None = None  # "not a repo", "no commits yet", ...


@dataclass
class Session:
    source: str  # claude-code | cursor | vscode
    session_id: str
    last_active_at: datetime
    title: str | None = None
    project_path: str | None = None
    started_at: datetime | None = None
    live: bool = False
    live_status: str | None = None
    live_state: str | None = None       # busy | waiting | idle
    waiting_for: str | None = None      # "permission prompt", "input needed"
    state_since: datetime | None = None # when live_state last changed
    branch: str | None = None
    model: str | None = None
    message_count: int | None = None
    last_user_text: str | None = None
    last_assistant_text: str | None = None
    todos: list[Todo] = field(default_factory=list)
    ended_mid_task: bool = False
    plan_steps: list[str] = field(default_factory=list)
    plan_name: str | None = None
    extras: dict = field(default_factory=dict)
    git: GitState | None = None
    # filled in later by the summarizer
    recap: str | None = None
    next_step: str | None = None

    @property
    def project_name(self) -> str:
        if not self.project_path:
            return "(unknown project)"
        return self.project_path.rstrip("/").rsplit("/", 1)[-1] or self.project_path

    @property
    def blocked(self) -> bool:
        """Live, but stalled waiting on the human rather than working."""
        return self.live and self.live_state == "waiting"

    @property
    def state_minutes(self) -> int | None:
        if self.state_since is None:
            return None
        return max(0, int((now() - self.state_since).total_seconds() // 60))

    @property
    def open_todos(self) -> list[Todo]:
        return [t for t in self.todos if t.is_open]

    def fingerprint(self) -> str:
        """Identity of the session's *content*.

        Used to skip re-summarizing and re-embedding sessions that have not
        changed since the last run.
        """
        payload = json.dumps(
            [
                self.last_active_at.isoformat(),
                self.message_count,
                self.last_user_text,
                self.last_assistant_text,
                [(t.text, t.status, t.origin, t.verified) for t in self.todos],
            ],
            sort_keys=True,
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["project_name"] = self.project_name
        d["blocked"] = self.blocked
        d["state_minutes"] = self.state_minutes
        for key in ("last_active_at", "started_at", "state_since"):
            if d.get(key) is not None:
                d[key] = getattr(self, key).isoformat()
        return d


def utc(ts_ms: float | int | None) -> datetime | None:
    """Epoch milliseconds -> aware UTC datetime."""
    if not ts_ms:
        return None
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)


def now() -> datetime:
    return datetime.now(timezone.utc)
