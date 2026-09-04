"""Read work items out of plan-mode documents.

Claude Code's task tools are unused in practice, so its plan documents are the
only real record of intended work. They are prose plans, though, not
checklists: there are no checkboxes and nothing marks a step as done. So this
module only extracts *candidate* steps; deciding which remain is inference,
done later against what the session actually did, and is labelled as such.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import config

# Headings whose body is a list of work to do, rather than context or analysis.
_WORK_HEADING = re.compile(
    r"^#{2,4}\s+.*\b("
    r"build order|next steps?|implementation|remaining|to ?do|todos|"
    r"roadmap|milestones?|work items?|phases?|plan of attack|steps?"
    r")\b",
    re.IGNORECASE,
)

# A phase heading is itself a work item when it has no list under it.
_PHASE_HEADING = re.compile(r"^#{2,4}\s+(phase\s+\S+.*)$", re.IGNORECASE)

_LIST_ITEM = re.compile(r"^(\s*)(?:(\d+)\.|[-*+])\s+(.*)$")
_HEADING = re.compile(r"^#{1,6}\s")

MAX_STEPS = 15
MAX_CHARS = 180


def find(session_extras: dict | None) -> Path | None:
    """The plan document for a session, via the `slug` it records."""
    slug = (session_extras or {}).get("plan_slug")
    if not slug:
        return None
    candidate = config.CLAUDE_DIR / "plans" / f"{slug}.md"
    return candidate if candidate.is_file() else None


def _clean(text: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", text)          # inline code
    text = re.sub(r"\*\*([^*]*)\*\*", r"\1", text)    # bold
    text = re.sub(r"(?<!\w)[*_]([^*_]+)[*_](?!\w)", r"\1", text)  # emphasis
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)          # links
    text = " ".join(text.split())
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rstrip() + "…"
    return text


def extract_steps(markdown: str) -> list[str]:
    """Candidate work items, in document order, de-duplicated."""
    lines = markdown.splitlines()
    steps: list[str] = []
    seen: set[str] = set()

    in_work_section = False
    pending: list[str] | None = None   # the item currently being accumulated
    fenced = False

    def flush() -> None:
        nonlocal pending
        if pending:
            text = _clean(" ".join(pending))
            key = text.lower()
            if text and key not in seen:
                seen.add(key)
                steps.append(text)
        pending = None

    for line in lines:
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if fenced:
            continue

        if _HEADING.match(line):
            flush()
            phase = _PHASE_HEADING.match(line)
            if phase:
                # "## Phase 3 — Ledger integrity" is one work item. Its body is
                # implementation detail, not a list of further steps, so we
                # deliberately do not descend into it.
                in_work_section = False
                text = _clean(phase.group(1))
                if text and text.lower() not in seen:
                    seen.add(text.lower())
                    steps.append(text)
            else:
                in_work_section = bool(_WORK_HEADING.match(line))
            continue

        if not in_work_section:
            continue

        item = _LIST_ITEM.match(line)
        if item:
            indent, ordinal, body = item.groups()
            # Only top-level items; nested bullets are detail, not steps.
            if len(indent) <= 1:
                flush()
                pending = [body]
            continue

        if pending is not None:
            if line.strip():
                pending.append(line.strip())   # wrapped continuation line
            else:
                flush()

    flush()
    return steps[:MAX_STEPS]


def steps_for(session_extras: dict | None) -> tuple[list[str], str | None]:
    """(steps, plan filename) for a session, or ([], None)."""
    path = find(session_extras)
    if not path:
        return [], None
    try:
        markdown = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], None
    return extract_steps(markdown), path.name


# Directories that are huge and never informative about plan progress.
_SKIP_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".next", "dist",
    "build", ".build", "target", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "DerivedData", "Pods", ".gradle", ".idea", "coverage", ".terraform",
}
_MAX_INDEXED = 6000

# A file-ish token: has an extension, no spaces. Also bare directory refs.
_PATH_TOKEN = re.compile(r"\b([\w.\-]+(?:/[\w.\-]+)*\.[A-Za-z][\w]{0,5})\b")
_NOT_A_PATH = re.compile(
    r"^(?:e\.g|i\.e|etc|vs|no|v?\d+(?:\.\d+)*)\.?$|^\d", re.IGNORECASE
)

_index_cache: dict[str, tuple[set[str], set[str]]] = {}


def _project_index(project_path: str) -> tuple[set[str], set[str]]:
    """(relative paths, basenames) of files in a project, cheaply and capped."""
    cached = _index_cache.get(project_path)
    if cached is not None:
        return cached

    root = Path(project_path)
    relative: set[str] = set()
    basenames: set[str] = set()
    if root.is_dir():
        count = 0
        for current, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
            base = Path(current).relative_to(root)
            for name in filenames:
                relative.add(str(base / name) if str(base) != "." else name)
                basenames.add(name)
                count += 1
                if count >= _MAX_INDEXED:
                    break
            if count >= _MAX_INDEXED:
                break

    _index_cache[project_path] = (relative, basenames)
    return relative, basenames


def file_evidence(step: str, project_path: str | None) -> dict[str, bool]:
    """Which files named by a plan step already exist in the project.

    Plan steps in a build order usually name the artefacts they produce, so
    this turns "did this happen?" into a fact instead of a guess. It is
    evidence for the model, not a verdict: a file can exist while the step is
    only half done.
    """
    if not project_path:
        return {}
    relative, basenames = _project_index(project_path)

    found: dict[str, bool] = {}
    for token in _PATH_TOKEN.findall(step):
        if _NOT_A_PATH.match(token) or len(token) < 4:
            continue
        exists = (
            token in relative
            or token in basenames
            or any(path == token or path.endswith("/" + token) for path in relative)
        )
        found[token] = exists
        if len(found) >= 6:
            break
    return found


def assess(steps: list[str], project_path: str | None) -> list[tuple[str, str, bool]]:
    """Judge plan steps as (text, status, verified).

    Deliberately not a model call. Given file evidence an 8B model is roughly
    as good as the check itself; without it, the model was observed marking
    plainly-unstarted work as done. Under-reporting defeats the point of the
    tool, so the rule is: decide from disk where we can, and otherwise show the
    step as outstanding and flag it unverified.
    """
    out: list[tuple[str, str, bool]] = []
    for text in steps:
        evidence = file_evidence(text, project_path)
        if evidence and all(evidence.values()):
            out.append((text, "completed", True))
        elif evidence:
            out.append((text, "pending", True))
        else:
            out.append((text, "pending", False))
    return out
