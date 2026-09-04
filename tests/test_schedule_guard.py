"""launchd cannot read ~/Documents, so scheduling from there must be refused."""

import pathlib
import sys
from pathlib import Path

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from agent_recap import schedule


def test_protected_executable_is_refused():
    home = Path.home()
    reason = schedule.protected_reason(str(home / "Documents/proj/.venv/bin/agent-recap"))
    assert reason and "Documents" in reason
    assert "uv tool install" in reason


def test_unprotected_executable_is_allowed(monkeypatch):
    """A standalone install outside the protected dirs must pass both checks."""
    import agent_recap

    monkeypatch.setattr(
        agent_recap, "__file__", str(Path.home() / ".local/share/uv/tools/x/agent_recap/__init__.py")
    )
    assert schedule.protected_reason(str(Path.home() / ".local/bin/agent-recap")) is None


def test_editable_install_is_refused(monkeypatch):
    """The launcher can be safe while the package source is not."""
    import agent_recap

    monkeypatch.setattr(
        agent_recap, "__file__",
        str(Path.home() / "Documents/emi-random/agent-recap/agent_recap/__init__.py"),
    )
    reason = schedule.protected_reason(str(Path.home() / ".local/bin/agent-recap"))
    assert reason and "package source" in reason


def test_protected_dirs_cover_the_tcc_set():
    assert set(schedule.PROTECTED) >= {"Documents", "Desktop", "Downloads"}


def test_status_reports_a_missing_scheduled_binary(tmp_path, monkeypatch):
    """A shared ~/.local/bin symlink can be removed by another installer.

    The schedule then points at nothing and fails silently at run time, so
    status has to surface it.
    """
    import plistlib

    plist_path = tmp_path / "com.agent-recap.daily.plist"
    with plist_path.open("wb") as handle:
        plistlib.dump(
            {
                "Label": "com.agent-recap.daily",
                "ProgramArguments": [str(tmp_path / "gone" / "agent-recap"), "recap"],
                "StartCalendarInterval": {"Hour": 8, "Minute": 30},
            },
            handle,
        )
    monkeypatch.setattr(schedule, "PLIST_PATH", plist_path)

    info = schedule.status()
    assert info["installed"]
    assert info["at"] == "08:30"
    assert info["target_exists"] is False


def test_status_reports_an_existing_binary(tmp_path, monkeypatch):
    import plistlib

    binary = tmp_path / "agent-recap"
    binary.write_text("#!/bin/sh\n")
    plist_path = tmp_path / "p.plist"
    with plist_path.open("wb") as handle:
        plistlib.dump(
            {"Label": "x", "ProgramArguments": [str(binary)],
             "StartCalendarInterval": {"Hour": 7, "Minute": 0}},
            handle,
        )
    monkeypatch.setattr(schedule, "PLIST_PATH", plist_path)
    assert schedule.status()["target_exists"] is True


def test_status_when_nothing_is_installed(tmp_path, monkeypatch):
    monkeypatch.setattr(schedule, "PLIST_PATH", tmp_path / "absent.plist")
    info = schedule.status()
    assert info["installed"] is False


def test_guidance_names_the_project_not_the_cwd(tmp_path, monkeypatch):
    """The message must not tell the user to install whatever directory they
    happen to be standing in."""
    import agent_recap

    project = Path.home() / "Documents" / "somewhere" / "agent-recap"
    monkeypatch.setattr(
        agent_recap, "__file__", str(project / "agent_recap" / "__init__.py")
    )
    monkeypatch.chdir(tmp_path)

    reason = schedule.protected_reason(str(Path.home() / ".local/bin/agent-recap"))
    assert reason is not None
    assert str(project) in reason
    assert str(tmp_path) not in reason
