"""Turn raw session state into a short recap plus a likely next step.

Runs entirely on the local Ollama instance. Two things keep it fast:
batching several sessions per generate call, and a fingerprint cache that skips
sessions whose content has not changed since the last run.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone

from .llm.ollama import Ollama, OllamaError
from .models import Session
from .scrub import scrub

SYSTEM = (
    "You summarize a developer's unfinished coding sessions so they can resume work.\n"
    "For each session you are given, write:\n"
    '  "recap": 1-2 sentences on what was being done and where it stopped.\n'
    '  "next_step": the single most likely next action, imperative, or null if clearly finished.\n'
    "Rules: be concrete. Name the files, branches, errors or todos you were given.\n"
    "Write for the person who did the work, so start with the work itself and use\n"
    "plain past tense. Never open with \"The developer\", \"The user\", \"The session\"\n"
    "or \"This session\" -- write \"Wired up the chunker...\", not \"The developer was\n"
    "wiring up the chunker...\".\n"
    "Prefer specifics from `recent_turns` and `files_touched` over restating the title.\n"
    "Never invent files, errors or outcomes that are not in the input. If the input is\n"
    "too thin to tell, say so plainly in the recap rather than guessing.\n"
    "`confirmed_unfinished_plan_steps`, when present, is work verified as not yet\n"
    "done; use it to pick a sensible next_step and do not contradict it. Steps not\n"
    "listed there must not be described as unfinished.\n"
    'Reply with JSON only, shaped {"<id>": {"recap": "...", "next_step": "..."}}, '
    "using the exact session ids given."
)


def _excerpt(text: str | None, limit: int) -> str | None:
    text = scrub(text)
    if not text:
        return None
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


def _payload(session: Session, limit: int) -> dict:
    """Everything the model gets about one session, scrubbed and clipped."""
    extras = session.extras or {}
    turns = extras.get("turns") or []
    # A per-turn budget, so a handful of turns cannot blow past the excerpt cap.
    per_turn = max(200, limit // max(1, len(turns))) if turns else limit

    payload = {
        "id": session.session_id,
        "tool": session.source,
        "project": session.project_name,
        "title": scrub(session.title),
        "branch": session.branch,
        "stopped_mid_task": session.ended_mid_task,
        "open_todos": [t.text for t in session.open_todos][:8],
    }
    if turns:
        payload["recent_turns"] = [
            {"from": role, "text": _excerpt(text, per_turn)} for role, text in turns
        ]
    else:
        payload["last_user_message"] = _excerpt(session.last_user_text, limit)
        payload["last_assistant_message"] = _excerpt(session.last_assistant_text, limit)

    # Only steps we *confirmed* unfinished (their files are missing) may shape
    # the recap. Unverified steps mean "status unknown", and feeding those in as
    # outstanding made the model narrate finished work as still in progress.
    confirmed = [t.text for t in session.open_todos if t.inferred and t.verified]
    if confirmed:
        payload["confirmed_unfinished_plan_steps"] = confirmed[:8]
    if session.plan_name:
        payload["plan_document"] = session.plan_name

    files = extras.get("files_touched") or []
    if files:
        payload["files_touched"] = [f.rsplit("/", 2)[-1] for f in files][:10]
    if extras.get("subtitle"):
        payload["editor_activity"] = scrub(extras["subtitle"])
    if session.git and session.git.dirty_files:
        payload["uncommitted_files"] = session.git.dirty_files
    return payload


def _clean_next_step(value) -> str | None:
    """Models sometimes emit the *string* "null"/"none" instead of JSON null."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() in {"null", "none", "n/a", "nothing", "-"}:
        return None
    return text


def _parse(raw: str) -> dict:
    """Tolerantly pull the JSON object out of a model response."""
    raw = (raw or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    start, end = raw.find("{"), raw.rfind("}")
    if start >= 0 and end > start:
        try:
            return json.loads(raw[start : end + 1])
        except json.JSONDecodeError:
            return {}
    return {}


def load_cached(conn: sqlite3.Connection, sessions: list[Session]) -> list[Session]:
    """Fill recaps from the cache; return the sessions still needing one."""
    stale = []
    for session in sessions:
        row = conn.execute(
            "SELECT recap, next_step, fingerprint FROM recaps WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()
        if row and row["fingerprint"] == session.fingerprint() and row["recap"]:
            session.recap = row["recap"]
            session.next_step = row["next_step"]
        else:
            stale.append(session)
    return stale


def save(conn: sqlite3.Connection, sessions: list[Session], model: str) -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """
        INSERT INTO recaps(session_id, recap, next_step, model, generated_at, fingerprint)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            recap=excluded.recap, next_step=excluded.next_step,
            model=excluded.model, generated_at=excluded.generated_at,
            fingerprint=excluded.fingerprint
        """,
        [
            (s.session_id, s.recap, s.next_step, model, stamp, s.fingerprint())
            for s in sessions
            if s.recap
        ],
    )
    conn.commit()


def deterministic(session: Session) -> None:
    """Fallback recap built only from parsed fields."""
    bits = []
    if session.title:
        bits.append(session.title)
    if session.open_todos:
        bits.append(f"{len(session.open_todos)} open todo(s)")
    if session.ended_mid_task:
        bits.append("stopped mid-task")
    session.recap = " · ".join(bits) if bits else "No summary available."
    if session.open_todos:
        session.next_step = session.open_todos[0].text


def run(
    sessions: list[Session],
    client: Ollama,
    *,
    batch_size: int = 6,
    excerpt_chars: int = 1200,
    verbose: bool = False,
) -> None:
    """Fill `recap`/`next_step` on each session, in place."""
    if not sessions:
        return

    for start in range(0, len(sessions), batch_size):
        batch = sessions[start : start + batch_size]
        prompt = (
            f"{SYSTEM}\n\nSESSIONS:\n"
            + json.dumps([_payload(s, excerpt_chars) for s in batch], indent=1)
        )
        answers: dict = {}
        for temperature in (0.2, 0.0):
            try:
                answers = _parse(client.generate(prompt, temperature=temperature))
            except OllamaError as exc:
                print(f"warning: summarizer unavailable: {exc}", file=sys.stderr)
                break
            if answers:
                break
            if verbose:
                print("  retrying batch at temperature 0", file=sys.stderr)

        for session in batch:
            entry = answers.get(session.session_id)
            if isinstance(entry, dict) and entry.get("recap"):
                session.recap = str(entry["recap"]).strip()
                session.next_step = _clean_next_step(entry.get("next_step"))
            else:
                deterministic(session)
        if verbose:
            done = min(start + batch_size, len(sessions))
            print(f"  summarized {done}/{len(sessions)}", file=sys.stderr)
