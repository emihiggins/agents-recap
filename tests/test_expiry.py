"""Tiered expiry: age alone must not evict work that is still open."""

import pathlib
import sys
from datetime import timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from agent_recap.models import now
from agent_recap.store import db, expiry, vectors


def _seed(conn, session_id, *, days_old, project, pinned=0, open_todo=False):
    stamp = (now() - timedelta(days=days_old)).isoformat()
    conn.execute(
        """INSERT INTO sessions(session_id, source, project_path, last_active_at,
                                pinned, fingerprint)
           VALUES (?,?,?,?,?,?)""",
        (session_id, "cursor", project, stamp, pinned, "fp"),
    )
    if open_todo:
        conn.execute(
            "INSERT INTO todos(session_id, idx, text, status) VALUES (?,0,?,?)",
            (session_id, "finish the thing", "in_progress"),
        )
    cur = conn.execute(
        """INSERT INTO chunks(session_id, source, kind, text, session_last_active)
           VALUES (?,?,?,?,?)""",
        (session_id, "cursor", "session_summary", "text", stamp),
    )
    vectors.upsert(conn, cur.lastrowid, [0.1] * 768)
    conn.commit()


def _store(tmp_path):
    conn = db.connect(tmp_path / "s.db")
    live_dir = str(tmp_path)  # a directory that exists
    gone_dir = str(tmp_path / "deleted-project")
    _seed(conn, "pinned-old", days_old=400, project=live_dir, pinned=1)
    _seed(conn, "open-todo-old", days_old=400, project=live_dir, open_todo=True)
    _seed(conn, "recent", days_old=3, project=live_dir)
    _seed(conn, "stale", days_old=200, project=live_dir)
    _seed(conn, "missing-dir", days_old=1, project=gone_dir)
    return conn


def test_classification_by_tier(tmp_path):
    conn = _store(tmp_path)
    verdict = expiry.classify(conn, max_age_days=90)
    keep = dict(verdict["keep"])
    drop = dict(verdict["drop"])

    assert keep["pinned-old"] == expiry.KEEP_PINNED
    assert keep["open-todo-old"] == expiry.KEEP_OPEN_TODO
    assert keep["recent"] == expiry.KEEP_RECENT
    assert drop["stale"] == expiry.DROP_STALE
    # Recent, but its project is gone -- dropped regardless of age.
    assert drop["missing-dir"] == expiry.DROP_MISSING


def test_dry_run_changes_nothing(tmp_path):
    conn = _store(tmp_path)
    before = db.stats(conn)
    expiry.prune(conn, 90, dry_run=True)
    assert db.stats(conn) == before


def test_prune_cascades_to_vectors(tmp_path):
    conn = _store(tmp_path)
    assert db.stats(conn)["vectors"] == 5
    expiry.prune(conn, 90)
    stats = db.stats(conn)
    assert stats["sessions"] == 3
    # The vec0 virtual table needs an explicit cascade or vectors orphan.
    assert stats["chunks"] == 3
    assert stats["vectors"] == 3


def test_forget_removes_everything_for_a_session(tmp_path):
    conn = _store(tmp_path)
    expiry.forget(conn, ["open-todo-old"])
    assert conn.execute(
        "SELECT COUNT(*) FROM todos WHERE session_id='open-todo-old'"
    ).fetchone()[0] == 0
    assert db.stats(conn)["vectors"] == 4


def test_pinning_survives_pruning(tmp_path):
    conn = _store(tmp_path)
    assert expiry.set_pinned(conn, "stale", True)
    expiry.prune(conn, 90)
    assert conn.execute(
        "SELECT COUNT(*) FROM sessions WHERE session_id='stale'"
    ).fetchone()[0] == 1


def test_set_pinned_on_unknown_session_reports_failure(tmp_path):
    conn = db.connect(tmp_path / "s.db")
    assert expiry.set_pinned(conn, "nope", True) is False
