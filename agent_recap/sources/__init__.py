"""Session collectors, one per AI surface."""

from __future__ import annotations

import sys

from . import claude_code, cursor, git_state, vscode

PROBES = {
    "claude-code": claude_code.probe,
    "cursor": cursor.probe,
    "vscode": vscode.probe,
}

COLLECTORS = {
    "claude-code": claude_code.collect,
    "cursor": cursor.collect,
    "vscode": vscode.collect,
}


def collect_all(sources, window_days: int, *, verbose: bool = False):
    """Collect from each named source, isolating failures.

    One unreadable store must never take down the whole run, so every
    collector's exceptions are caught and reported rather than propagated.
    """
    sessions = []
    for name in sources:
        collector = COLLECTORS.get(name)
        if collector is None:
            print(f"warning: unknown source {name!r}", file=sys.stderr)
            continue
        try:
            found = collector(window_days)
        except Exception as exc:  # noqa: BLE001 - deliberate isolation boundary
            print(f"warning: source {name!r} failed: {exc}", file=sys.stderr)
            continue
        if verbose:
            print(f"  {name}: {len(found)} sessions", file=sys.stderr)
        if not found:
            # Zero sessions is normal for an unused tool, but suspicious when
            # the store exists and holds data -- that suggests format drift.
            report = PROBES[name]()
            if report["present"] and not report["healthy"]:
                print(f"warning: {name} returned nothing and looks changed: "
                      f"{report['detail']} (run `agent-recap doctor`)", file=sys.stderr)
        sessions.extend(found)
    git_state.annotate(sessions)
    return sessions


def rank(sessions, limit: int | None = None):
    """Most-actionable first: blocked on you, live, unfinished work, recency.

    Live and open-todo sessions therefore survive the cap even when they are
    older than the newest chatter.
    """
    ordered = sorted(
        sessions,
        key=lambda s: (
            s.blocked,
            s.live,
            bool(s.open_todos),
            s.ended_mid_task,
            s.last_active_at,
        ),
        reverse=True,
    )
    return ordered[:limit] if limit else ordered
