"""Install a launchd agent so the recap is ready before you sit down.

The scheduled run writes the HTML without stealing focus; you open it when you
want it. Output and errors go to log files under the data directory so a
failing scheduled run is diagnosable rather than silent.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

from . import config

LABEL = "com.agent-recap.daily"
PLIST_PATH = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def _executable() -> str:
    """Absolute path to the installed `agent-recap` binary."""
    found = shutil.which("agent-recap")
    if found:
        return found
    # Running from a venv or a source checkout.
    sibling = Path(sys.executable).parent / "agent-recap"
    if sibling.exists():
        return str(sibling)
    return f"{sys.executable} -m agent_recap"


# macOS TCC withholds these directories from launchd agents, so a scheduled job
# whose interpreter lives here dies with EPERM before running any of our code.
PROTECTED = ("Documents", "Desktop", "Downloads")


def _in_protected(path: Path) -> str | None:
    home = Path.home()
    for name in PROTECTED:
        if path.is_relative_to(home / name):
            return name
    return None


def protected_reason(executable: str) -> str | None:
    """Explain why launchd cannot run this install, or None if it can.

    Both the launcher *and* the package source have to be outside the
    protected directories. An editable install passes the first check and
    fails the second, because the interpreter still imports from the checkout.
    """
    import agent_recap

    candidates = []
    try:
        candidates.append(("launcher", Path(executable.split(" ")[0]).resolve()))
    except (OSError, RuntimeError):
        pass
    try:
        candidates.append(("package source", Path(agent_recap.__file__).resolve().parent))
    except (OSError, RuntimeError, AttributeError):
        pass

    # Derive the project root from the package itself, not the cwd: the user
    # may be running this from anywhere, and suggesting they install an
    # unrelated directory would be worse than saying nothing.
    try:
        project_root = Path(agent_recap.__file__).resolve().parent.parent
    except (OSError, RuntimeError, AttributeError):
        project_root = None

    for label, path in candidates:
        name = _in_protected(path)
        if name:
            target = str(project_root) if project_root else "<project directory>"
            return (
                f"the {label} ({path}) lives under ~/{name}, which macOS hides from "
                "scheduled jobs.\nInstall a standalone copy outside it:\n"
                f"  uv tool install --force --reinstall {target}\n"
                "(a plain install, not --editable, which would still import from here)\n"
                "then re-run `agent-recap schedule --at HH:MM`."
            )
    return None


def _program(days: int, limit: int, summarizer: str) -> list[str]:
    executable = _executable()
    parts = executable.split(" ") if " " in executable else [executable]
    return [
        *parts, "recap",
        "--days", str(days),
        "--limit", str(limit),
        "--summarizer", summarizer,
        "--no-open",
    ]


def install(*, hour: int, minute: int, days: int, limit: int, summarizer: str) -> Path:
    reason = protected_reason(_executable())
    if reason:
        raise RuntimeError(reason)
    data_dir = config.ensure_data_dir()
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)

    plist = {
        "Label": LABEL,
        "ProgramArguments": _program(days, limit, summarizer),
        "StartCalendarInterval": {"Hour": hour, "Minute": minute},
        "StandardOutPath": str(data_dir / "schedule.log"),
        "StandardErrorPath": str(data_dir / "schedule.err.log"),
        "RunAtLoad": False,
        # launchd starts with a bare environment; Ollama and the binary both
        # need a usable PATH.
        "EnvironmentVariables": {
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            + (f":{Path.home()}/.local/bin"),
            "HOME": str(Path.home()),
        },
    }
    with PLIST_PATH.open("wb") as handle:
        plistlib.dump(plist, handle)

    uid = os.getuid()
    # bootout first so re-installing picks up the new plist.
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
                   capture_output=True, text=True)
    proc = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{uid}", str(PLIST_PATH)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"launchctl bootstrap failed ({proc.returncode}): "
            f"{(proc.stderr or proc.stdout or '').strip()[:300]}"
        )
    return PLIST_PATH


def uninstall() -> bool:
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
                   capture_output=True, text=True)
    if PLIST_PATH.exists():
        PLIST_PATH.unlink()
        return True
    return False


def status() -> dict:
    uid = os.getuid()
    proc = subprocess.run(
        ["launchctl", "print", f"gui/{uid}/{LABEL}"], capture_output=True, text=True
    )
    info: dict = {"installed": PLIST_PATH.exists(), "loaded": proc.returncode == 0}
    if PLIST_PATH.exists():
        try:
            with PLIST_PATH.open("rb") as handle:
                plist = plistlib.load(handle)
            when = plist.get("StartCalendarInterval") or {}
            info["at"] = f"{when.get('Hour', 0):02d}:{when.get('Minute', 0):02d}"
            info["command"] = " ".join(plist.get("ProgramArguments") or [])
        except (OSError, plistlib.InvalidFileException):
            pass
        # A shared ~/.local/bin symlink can be clobbered by another installer,
        # leaving the schedule pointing at nothing. Fail loudly, not at 08:30.
        args = plist.get("ProgramArguments") or []
        if args:
            info["target"] = args[0]
            info["target_exists"] = Path(args[0]).exists()

    log = config.DATA_DIR / "schedule.log"
    if log.exists():
        info["last_run"] = log.stat().st_mtime
        info["log"] = str(log)
    return info
