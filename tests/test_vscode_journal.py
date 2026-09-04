"""The newer VS Code chat format is an append-only journal, not a document."""

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from agent_recap.sources.vscode import _load_session_doc, _parse_doc, _replay_journal


def _write(tmp_path, records):
    path = tmp_path / "s.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records))
    return path


def test_snapshot_then_set_and_append(tmp_path):
    path = _write(tmp_path, [
        {"kind": 0, "v": {"sessionId": "abc", "customTitle": "old", "requests": []}},
        {"kind": 1, "k": ["customTitle"], "v": "new title"},
        {"kind": 2, "k": ["requests"], "v": [{"message": {"text": "hi"}, "response": []}]},
        {"kind": 1, "k": ["requests", 0, "response"],
         "v": [{"value": "hello back"}]},
    ])
    doc = _replay_journal(path)
    assert doc["customTitle"] == "new title"
    assert len(doc["requests"]) == 1
    assert doc["requests"][0]["response"][0]["value"] == "hello back"


def test_parse_extracts_both_sides(tmp_path):
    path = _write(tmp_path, [
        {"kind": 0, "v": {"sessionId": "abc", "customTitle": "T",
                          "lastMessageDate": 1767728397164, "requests": []}},
        {"kind": 2, "k": ["requests"], "v": [
            {"message": {"text": "fix the parser"},
             "response": [{"value": "I changed two lines."}]}]},
    ])
    session = _parse_doc(_load_session_doc(path), path, "/tmp/proj")
    assert session.last_user_text == "fix the parser"
    assert session.last_assistant_text == "I changed two lines."
    assert session.title == "T"


def test_todo_list_is_extracted(tmp_path):
    path = _write(tmp_path, [
        {"kind": 0, "v": {"sessionId": "abc", "lastMessageDate": 1767728397164, "requests": [
            {"message": {"text": "plan it"}, "response": [
                {"kind": "toolInvocationSerialized", "toolId": "manage_todo_list",
                 "toolSpecificData": {"kind": "todoList", "todoList": [
                     {"id": "1", "title": "Install dep", "status": "completed"},
                     {"id": "2", "title": "Wire it up", "status": "in-progress"},
                     {"id": "3", "title": "Write tests", "status": "not-started"}]}}]}]}},
    ])
    session = _parse_doc(_load_session_doc(path), path, "/tmp/proj")
    assert [t.status for t in session.todos] == ["completed", "in_progress", "pending"]
    assert len(session.open_todos) == 2
    assert session.ended_mid_task


def test_malformed_lines_are_skipped(tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text(
        '{"kind":0,"v":{"sessionId":"a","requests":[]}}\n'
        "not json at all\n"
        '{"kind":1,"k":["customTitle"],"v":"survived"}\n'
        '{"kind":9,"k":["ignored"],"v":1}\n'
    )
    assert _replay_journal(path)["customTitle"] == "survived"


def test_response_parts_without_kind_are_read(tmp_path):
    """Plain markdown parts carry no `kind`; that is the common case."""
    path = _write(tmp_path, [
        {"kind": 0, "v": {"sessionId": "a", "lastMessageDate": 1767728397164, "requests": [
            {"message": {"text": "q"}, "response": [
                {"kind": "prepareToolInvocation"},
                {"value": "part one"},
                {"value": {"value": "part two"}}]}]}},
    ])
    session = _parse_doc(_load_session_doc(path), path, "/tmp/p")
    assert "part one" in session.last_assistant_text
    assert "part two" in session.last_assistant_text
