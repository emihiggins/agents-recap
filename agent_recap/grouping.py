"""Collapse sessions into one entry per project.

A single project is often worked on from several sessions at once -- two Claude
Code terminals, or a Cursor composer alongside them -- but the question being
asked is "where did I leave off on X", which is per project. Grouping also
gives each project one git state and one merged todo list instead of repeating
them per card.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import GitState, Session, Todo


@dataclass
class ProjectGroup:
    key: str
    name: str
    project_path: str | None
    sessions: list[Session] = field(default_factory=list)
    trimmed: int = 0  # sessions dropped by rank(), shown as a count only

    @property
    def lead(self) -> Session:
        """The session that best represents the project's current state."""
        return self.sessions[0]

    @property
    def live(self) -> bool:
        return any(s.live for s in self.sessions)

    @property
    def blocked(self) -> bool:
        return any(s.blocked for s in self.sessions)

    @property
    def blocked_sessions(self) -> list[Session]:
        return [s for s in self.sessions if s.blocked]

    @property
    def ended_mid_task(self) -> bool:
        return any(s.ended_mid_task for s in self.sessions)

    @property
    def last_active_at(self):
        return max(s.last_active_at for s in self.sessions)

    @property
    def git(self) -> GitState | None:
        for session in self.sessions:
            if session.git:
                return session.git
        return None

    @property
    def plan_name(self) -> str | None:
        for session in self.sessions:
            if session.plan_name:
                return session.plan_name
        return None

    @property
    def sources(self) -> list[str]:
        seen = []
        for session in self.sessions:
            if session.source not in seen:
                seen.append(session.source)
        return seen

    @property
    def open_todos(self) -> list[Todo]:
        """Merged open todos, de-duplicated across sessions by text."""
        out: list[Todo] = []
        seen: set[str] = set()
        for session in self.sessions:
            for todo in session.open_todos:
                marker = todo.text.strip().lower()
                if marker in seen:
                    continue
                seen.add(marker)
                out.append(todo)
        return out

    @property
    def done_todos(self) -> list[Todo]:
        out: list[Todo] = []
        seen: set[str] = set()
        for session in self.sessions:
            for todo in session.todos:
                if todo.is_open:
                    continue
                marker = todo.text.strip().lower()
                if marker in seen:
                    continue
                seen.add(marker)
                out.append(todo)
        return out


def _session_rank(session: Session) -> tuple:
    return (
        session.blocked,
        session.live,
        bool(session.open_todos),
        session.ended_mid_task,
        session.last_active_at,
    )


def group(sessions: list[Session]) -> list[ProjectGroup]:
    """One group per project path, sessions inside ranked by actionability."""
    groups: dict[str, ProjectGroup] = {}
    for session in sessions:
        # Fall back to the session id so unknown-project sessions stay separate
        # rather than collapsing into one meaningless bucket.
        key = session.project_path or f"~unknown~{session.session_id}"
        existing = groups.get(key)
        if existing is None:
            existing = ProjectGroup(
                key=key, name=session.project_name, project_path=session.project_path
            )
            groups[key] = existing
        existing.sessions.append(session)

    for entry in groups.values():
        entry.sessions.sort(key=_session_rank, reverse=True)
    return list(groups.values())


def rank(groups: list[ProjectGroup], limit: int | None = None,
         *, max_per_project: int = 4) -> list[ProjectGroup]:
    """Blocked first, then live, then unfinished work, then recency.

    Each group keeps only its most actionable sessions: a project worked on
    from seven sessions does not need seven recaps generated for it.
    """
    ordered = sorted(
        groups,
        key=lambda g: (g.blocked, g.live, bool(g.open_todos), g.ended_mid_task, g.last_active_at),
        reverse=True,
    )
    ordered = ordered[:limit] if limit else ordered
    if max_per_project:
        for entry in ordered:
            entry.trimmed = max(0, len(entry.sessions) - max_per_project)
            entry.sessions = entry.sessions[:max_per_project]
    return ordered
