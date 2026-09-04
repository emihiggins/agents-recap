"""Working-tree state per project directory.

A best-effort signal: many project directories are not repositories, or are
repositories with no commits yet, so every call has to tolerate failure and
report a note instead of raising.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from ..models import GitState

_cache: dict[str, GitState | None] = {}


def _git(cwd: str, *args: str) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return 1, ""
    return proc.returncode, proc.stdout.strip()


def for_path(project_path: str | None) -> GitState | None:
    if not project_path:
        return None
    if project_path in _cache:
        return _cache[project_path]

    result: GitState | None
    if not Path(project_path).is_dir():
        result = GitState(note="directory no longer exists")
    else:
        code, _ = _git(project_path, "rev-parse", "--is-inside-work-tree")
        if code != 0:
            result = GitState(note="not a git repo")
        else:
            code, branch = _git(project_path, "rev-parse", "--abbrev-ref", "HEAD")
            if code != 0 or not branch:
                # A fresh repo with no commits cannot resolve HEAD.
                _, status = _git(project_path, "status", "--porcelain")
                dirty = len([l for l in status.splitlines() if l.strip()])
                result = GitState(dirty_files=dirty, note="no commits yet")
            else:
                _, status = _git(project_path, "status", "--porcelain")
                dirty = len([l for l in status.splitlines() if l.strip()])
                _, commit = _git(project_path, "log", "--oneline", "-1")
                ahead_code, ahead = _git(project_path, "rev-list", "--count", "@{u}..HEAD")
                result = GitState(
                    branch=branch,
                    dirty_files=dirty,
                    last_commit=commit or None,
                    ahead=int(ahead) if ahead_code == 0 and ahead.isdigit() else None,
                    note=None if ahead_code == 0 else "no upstream",
                )

    _cache[project_path] = result
    return result


def annotate(sessions) -> None:
    """Attach git state in place, running at most one git pass per directory."""
    for session in sessions:
        session.git = for_path(session.project_path)
