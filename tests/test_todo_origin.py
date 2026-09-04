"""Reported todos and inferred plan steps must not be conflated."""

import pathlib
import sys
from datetime import timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from agent_recap import grouping, summarize
from agent_recap.models import Session, Todo, now
from agent_recap.store import db, expiry, vectors


def test_origin_and_verified_defaults():
    todo = Todo("x", "pending")
    assert todo.origin == "tool" and todo.verified and not todo.inferred


def test_inferred_flag():
    assert Todo("x", "pending", origin="plan").inferred


def test_fingerprint_changes_when_origin_changes():
    """Otherwise a session would keep a stale recap after reclassification."""
    base = dict(source="claude-code", session_id="a", last_active_at=now())
    tool = Session(**base, todos=[Todo("x", "pending")])
    plan = Session(**base, todos=[Todo("x", "pending", origin="plan")])
    assert tool.fingerprint() != plan.fingerprint()


def test_only_verified_unfinished_steps_reach_the_summarizer():
    """Unknown status must not be described to the model as outstanding."""
    session = Session(
        source="claude-code", session_id="a", last_active_at=now(),
        plan_name="p.md",
        todos=[
            Todo("confirmed missing", "pending", origin="plan", verified=True),
            Todo("cannot tell", "pending", origin="plan", verified=False),
            Todo("already done", "completed", origin="plan", verified=True),
        ],
    )
    payload = summarize._payload(session, 500)
    assert payload["confirmed_unfinished_plan_steps"] == ["confirmed missing"]
    assert payload["plan_document"] == "p.md"


def test_no_plan_key_when_nothing_confirmed():
    session = Session(
        source="claude-code", session_id="a", last_active_at=now(),
        todos=[Todo("cannot tell", "pending", origin="plan", verified=False)],
    )
    assert "confirmed_unfinished_plan_steps" not in summarize._payload(session, 500)


def test_group_exposes_the_plan_document():
    session = Session(source="claude-code", session_id="a", last_active_at=now(),
                      project_path="/p/one", plan_name="plan.md")
    assert grouping.group([session])[0].plan_name == "plan.md"


def _seed(conn, session_id, *, days_old, project, origin):
    stamp = (now() - timedelta(days=days_old)).isoformat()
    conn.execute(
        "INSERT INTO sessions(session_id, source, project_path, last_active_at,"
        " pinned, fingerprint) VALUES (?,?,?,?,0,'fp')",
        (session_id, "claude-code", project, stamp),
    )
    conn.execute(
        "INSERT INTO todos(session_id, idx, text, status, origin, verified)"
        " VALUES (?,0,'work','pending',?,1)",
        (session_id, origin),
    )
    cur = conn.execute(
        "INSERT INTO chunks(session_id, source, kind, text, session_last_active)"
        " VALUES (?,'claude-code','x','t',?)",
        (session_id, stamp),
    )
    vectors.upsert(conn, cur.lastrowid, [0.1] * 768)
    conn.commit()


def test_inferred_todos_do_not_pin_context_forever(tmp_path):
    """Nearly every plan has a step nobody will do; honouring those would
    keep the whole store alive indefinitely."""
    conn = db.connect(tmp_path / "s.db")
    _seed(conn, "tool-todo", days_old=400, project=str(tmp_path), origin="tool")
    _seed(conn, "plan-step", days_old=400, project=str(tmp_path), origin="plan")

    verdict = expiry.classify(conn, max_age_days=90)
    assert dict(verdict["keep"])["tool-todo"] == expiry.KEEP_OPEN_TODO
    assert dict(verdict["drop"])["plan-step"] == expiry.DROP_STALE


def test_next_step_is_dropped_when_a_todo_already_says_it():
    """The work item is the better of the two: it carries a status flag."""
    from agent_recap import render

    session = Session(
        source="cursor", session_id="a", last_active_at=now(), project_path="/p/one",
        next_step="Final review pass over the new handler code",
        todos=[Todo("Final review pass over the new handler code", "in_progress")],
    )
    group = grouping.group([session])[0]
    assert render._duplicates_a_todo(session.next_step, group)
    assert "pick up here" not in render._row(group)


def test_next_step_is_kept_when_it_is_new_information():
    from agent_recap import render

    session = Session(
        source="cursor", session_id="a", last_active_at=now(), project_path="/p/one",
        next_step="Run the migration against staging",
        todos=[Todo("Write the changelog", "pending")],
    )
    group = grouping.group([session])[0]
    assert not render._duplicates_a_todo(session.next_step, group)
    assert "pick up here" in render._row(group)


def test_next_step_dedupe_ignores_punctuation_and_case():
    from agent_recap import render

    session = Session(
        source="cursor", session_id="a", last_active_at=now(), project_path="/p/one",
        next_step="Fix the parser.",
        todos=[Todo("fix the parser", "pending")],
    )
    assert render._duplicates_a_todo(session.next_step, grouping.group([session])[0])


def test_no_next_step_is_not_a_duplicate():
    from agent_recap import render

    session = Session(source="cursor", session_id="a", last_active_at=now(),
                      project_path="/p/one")
    assert not render._duplicates_a_todo(None, grouping.group([session])[0])
