"""Format-drift probes must fail loudly, not return zero sessions quietly."""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from agent_recap import sources
from agent_recap.sources import vscode


def test_every_source_has_a_probe():
    assert set(sources.PROBES) == set(sources.COLLECTORS)


def test_probes_report_the_expected_shape():
    for name, probe in sources.PROBES.items():
        report = probe()
        assert report["name"] == name
        assert isinstance(report["present"], bool)
        assert isinstance(report["healthy"], bool)
        assert report["detail"]


def test_probes_pass_on_this_machine():
    """These stores exist here, so a failure means real drift."""
    for name, probe in sources.PROBES.items():
        report = probe()
        assert report["healthy"], f"{name} drifted: {report['detail']}"


def test_vscode_probe_flags_an_unparseable_session(tmp_path, monkeypatch):
    directory = tmp_path / "hash" / "chatSessions"
    directory.mkdir(parents=True)
    (directory / "broken.json").write_text("{not json")
    monkeypatch.setattr(vscode, "WORKSPACE_STORAGE", tmp_path)
    report = vscode.probe()
    assert report["present"] and not report["healthy"]
    assert "cannot parse" in report["detail"]


def test_vscode_probe_flags_a_renamed_field(tmp_path, monkeypatch):
    directory = tmp_path / "hash" / "chatSessions"
    directory.mkdir(parents=True)
    (directory / "s.json").write_text(json.dumps(
        {"requests": [{"prompt": {"body": "renamed away"}}]}
    ))
    monkeypatch.setattr(vscode, "WORKSPACE_STORAGE", tmp_path)
    report = vscode.probe()
    assert not report["healthy"]
    assert "message.text" in report["detail"]


def test_vscode_probe_reports_absent_when_not_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(vscode, "WORKSPACE_STORAGE", tmp_path / "nope")
    report = vscode.probe()
    assert not report["present"] and report["healthy"]


def test_cursor_probe_tolerates_pathless_draft_composers(tmp_path, monkeypatch):
    """Unopened draft composers carry no workspace; that is not drift."""
    import json
    import sqlite3

    from agent_recap.sources import cursor as cursor_src

    db_path = tmp_path / "state.vscdb"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE composerHeaders (composerId TEXT PRIMARY KEY, workspaceId TEXT,"
        " createdAt INTEGER, lastUpdatedAt INTEGER, isArchived INTEGER,"
        " isSubagent INTEGER, recency INTEGER, checkpointAt INTEGER, value TEXT)"
    )
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    # Newest two are pathless drafts; an older one has a real path.
    rows = [
        ("draft-1", 300, json.dumps({"name": None})),
        ("draft-2", 200, json.dumps({"name": None})),
        ("real-1", 100, json.dumps(
            {"name": "x", "workspaceIdentifier": {"uri": {"fsPath": "/p/one"}}})),
    ]
    conn.executemany(
        "INSERT INTO composerHeaders(composerId, recency, value) VALUES (?,?,?)", rows
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(cursor_src, "GLOBAL_DB", db_path)
    report = cursor_src.probe()
    assert report["healthy"], report["detail"]
    assert "1/3 recent with a project path" in report["detail"]


def test_cursor_probe_flags_a_renamed_project_field(tmp_path, monkeypatch):
    import json
    import sqlite3

    from agent_recap.sources import cursor as cursor_src

    db_path = tmp_path / "state.vscdb"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE composerHeaders (composerId TEXT PRIMARY KEY, recency INTEGER,"
        " isArchived INTEGER, isSubagent INTEGER, value TEXT)"
    )
    conn.execute("CREATE TABLE cursorDiskKV (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute(
        "INSERT INTO composerHeaders(composerId, recency, value) VALUES (?,?,?)",
        ("a", 1, json.dumps({"name": "x", "folderPath": "/p/renamed-away"})),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(cursor_src, "GLOBAL_DB", db_path)
    report = cursor_src.probe()
    assert not report["healthy"]
    assert "fsPath" in report["detail"]
