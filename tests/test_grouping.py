"""Grouping must merge a project's sessions without hiding work."""

import pathlib
import sys
from datetime import timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from agent_recap import grouping
from agent_recap.models import Session, Todo, now


def mk(session_id, project, *, source="claude-code", minutes_ago=5, live=False,
       state=None, waiting=None, state_min=None, todos=(), mid=False):
    return Session(
        source=source,
        session_id=session_id,
        last_active_at=now() - timedelta(minutes=minutes_ago),
        project_path=project,
        live=live,
        live_state=state,
        waiting_for=waiting,
        state_since=now() - timedelta(minutes=state_min) if state_min is not None else None,
        todos=list(todos),
        ended_mid_task=mid,
    )


def test_sessions_collapse_by_project():
    groups = grouping.group([
        mk("a", "/p/one", minutes_ago=2), mk("b", "/p/one", source="cursor", minutes_ago=8),
        mk("c", "/p/two"),
    ])
    assert len(groups) == 2
    one = next(g for g in groups if g.name == "one")
    assert len(one.sessions) == 2
    # Newest first, so the claude-code session leads.
    assert one.sources == ["claude-code", "cursor"]


def test_unknown_projects_do_not_merge():
    """Sessions with no path must stay separate, not pile into one bucket."""
    groups = grouping.group([mk("a", None), mk("b", None)])
    assert len(groups) == 2


def test_blocked_session_makes_the_group_blocked():
    groups = grouping.group([
        mk("a", "/p/one"),
        mk("b", "/p/one", live=True, state="waiting", waiting="permission prompt", state_min=36),
    ])
    group = groups[0]
    assert group.blocked and group.live
    assert group.blocked_sessions[0].state_minutes == 36
    # The blocked session must lead, since it is the actionable one.
    assert group.lead.session_id == "b"


def test_open_todos_merge_and_deduplicate():
    shared = Todo(text="Run the tests", status="pending")
    groups = grouping.group([
        mk("a", "/p/one", todos=[shared, Todo("Ship it", "in_progress")]),
        mk("b", "/p/one", todos=[Todo("run the tests", "pending")]),
    ])
    texts = [t.text.lower() for t in groups[0].open_todos]
    assert texts.count("run the tests") == 1
    assert len(groups[0].open_todos) == 2


def test_done_todos_are_separated():
    groups = grouping.group([
        mk("a", "/p/one", todos=[Todo("Done thing", "completed"), Todo("Open thing", "pending")])
    ])
    assert [t.text for t in groups[0].open_todos] == ["Open thing"]
    assert [t.text for t in groups[0].done_todos] == ["Done thing"]


def test_rank_puts_blocked_above_live_above_todos():
    blocked = mk("bl", "/p/blocked", live=True, state="waiting", waiting="input", state_min=5)
    live = mk("lv", "/p/live", live=True, state="busy", minutes_ago=1)
    todo = mk("td", "/p/todo", todos=[Todo("x", "pending")], minutes_ago=2)
    stale = mk("st", "/p/stale", minutes_ago=9999)
    ranked = grouping.rank(grouping.group([stale, todo, live, blocked]))
    assert [g.name for g in ranked] == ["blocked", "live", "todo", "stale"]


def test_last_active_is_the_newest_session():
    groups = grouping.group([
        mk("old", "/p/one", minutes_ago=500), mk("new", "/p/one", minutes_ago=2),
    ])
    assert (now() - groups[0].last_active_at).total_seconds() < 200


def test_max_per_project_trims_but_records_the_count():
    sessions = [mk(f"s{i}", "/p/one", minutes_ago=i) for i in range(7)]
    ranked = grouping.rank(grouping.group(sessions), max_per_project=4)
    assert len(ranked[0].sessions) == 4
    assert ranked[0].trimmed == 3


def test_group_of_one_reports_no_trim():
    ranked = grouping.rank(grouping.group([mk("a", "/p/one")]))
    assert ranked[0].trimmed == 0
