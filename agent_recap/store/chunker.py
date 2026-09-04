"""Turn a session into a handful of retrievable chunks.

Raw transcripts are far too large to vectorize (~200 MB for Claude Code alone),
and most of that volume is tool output with no retrieval value. Instead each
session yields ~4-6 chunks built from state we already parsed, plus any plan
documents it produced. Plan files carry the actual decisions and rationale, so
they are the highest-value material for "where did I leave off?" questions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .. import config
from ..models import Session
from ..scrub import scrub

MAX_CHARS = 2000
PLAN_MIN_CHARS = 120

# Bump when chunk shapes change. The session fingerprint covers session
# *content*, so without this a chunker change leaves stale chunks in place on
# every session whose content happens not to have moved.
CHUNKER_VERSION = 2


@dataclass
class Chunk:
    session_id: str
    source: str
    project_path: str | None
    kind: str
    text: str

    @property
    def token_est(self) -> int:
        return max(1, len(self.text) // 4)


def _clip(text: str, limit: int = MAX_CHARS) -> str:
    text = " ".join(text.split())
    return text[:limit]


def _header(session: Session) -> str:
    """Prefix every chunk with project identity so the embedding carries it."""
    when = session.last_active_at.strftime("%Y-%m-%d")
    return f"[{session.project_name}] [{session.source}] [{when}]"


def _plan_files(session: Session) -> list[Path]:
    """Plan documents belonging to this session.

    Claude Code records a `slug` naming its plan file, which is an exact join.
    Cursor's `referencedPlans` is empty in practice, so its plans can only be
    matched heuristically on name -- best effort, and skipped when unclear.
    """
    out: list[Path] = []

    slug = (session.extras or {}).get("plan_slug")
    if slug:
        candidate = config.CLAUDE_DIR / "plans" / f"{slug}.md"
        if candidate.is_file():
            out.append(candidate)

    if session.source == "cursor" and config.CURSOR_PLANS.is_dir():
        name = _normalize(session.project_name)
        title = _normalize(session.title or "")
        for path in config.CURSOR_PLANS.glob("*.md"):
            # strip the trailing hash Cursor appends to plan filenames
            stem = _normalize(re.sub(r"[_-][0-9a-f]{8}$", "", path.stem.replace(".plan", "")))
            if len(stem) < 4:
                continue
            if stem in title or stem in name or (name and name in stem):
                out.append(path)

    return out[:3]


def _normalize(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _split_markdown(text: str) -> list[str]:
    """Split a plan on `##` headings, keeping each heading with its body."""
    parts = re.split(r"(?m)^(?=##\s)", text)
    return [p.strip() for p in parts if len(p.strip()) >= PLAN_MIN_CHARS]


def for_session(session: Session) -> list[Chunk]:
    head = _header(session)
    chunks: list[Chunk] = []

    def add(kind: str, body: str | None) -> None:
        body = scrub(body)
        if not body or not body.strip():
            return
        chunks.append(
            Chunk(
                session_id=session.session_id,
                source=session.source,
                project_path=session.project_path,
                kind=kind,
                text=f"{head} {_clip(body)}",
            )
        )

    # The primary retrieval target: what this session was and where it landed.
    summary_bits = [session.title or "", session.recap or "", session.next_step or ""]
    if session.branch:
        summary_bits.append(f"on branch {session.branch}")
    if session.project_path:
        summary_bits.append(f"in {session.project_path}")
    add("session_summary", " · ".join(b for b in summary_bits if b))

    add("last_user", session.last_user_text)
    add("last_assistant", session.last_assistant_text)

    reported = [t for t in session.open_todos if not t.inferred]
    if reported:
        add("todos", "Open work: " + "; ".join(f"[{t.status}] {t.text}" for t in reported))

    # Inferred steps are chunked separately and labelled, so a retrieved answer
    # cannot present a guess as the agent's own todo list.
    inferred = [t for t in session.open_todos if t.inferred]
    if inferred:
        confirmed = [t for t in inferred if t.verified]
        unknown = [t for t in inferred if not t.verified]
        lines = []
        if confirmed:
            lines.append("not started (files still missing): "
                         + "; ".join(t.text for t in confirmed))
        if unknown:
            lines.append("status unknown: " + "; ".join(t.text for t in unknown))
        add("plan_steps",
            f"Remaining steps from plan {session.plan_name or ''}: " + " | ".join(lines))

    for path in _plan_files(session):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for section in _split_markdown(text)[:12]:
            add("plan", f"Plan {path.name}: {section}")

    return chunks
