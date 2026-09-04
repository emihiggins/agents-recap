"""The store belongs to other applications; we must never be able to write it."""

import pathlib
import sqlite3
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from agent_recap import config
from agent_recap.sqlite_util import open_ro

CURSOR_DB = config.CURSOR_USER / "globalStorage/state.vscdb"
VSCODE_DB = config.VSCODE_USER / "globalStorage/state.vscdb"


@pytest.mark.parametrize("path", [CURSOR_DB, VSCODE_DB])
def test_connection_rejects_writes(path):
    if not path.exists():
        pytest.skip(f"{path} not present")
    with open_ro(path) as conn:
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' LIMIT 1"
        ).fetchone()
        assert table, "expected at least one table to read"
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(f'CREATE TABLE "agent_recap_probe" (x)')
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(f'DROP TABLE IF EXISTS "{table[0]}"')


def test_reads_return_data():
    if not CURSOR_DB.exists():
        pytest.skip("Cursor not installed")
    with open_ro(CURSOR_DB) as conn:
        count = conn.execute("SELECT COUNT(*) FROM composerHeaders").fetchone()[0]
        assert count >= 0
