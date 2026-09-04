"""Command line entry point."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from . import config, grouping, render, schedule, sources, summarize
from .llm.claude_cli import ClaudeCLI
from .llm.ollama import Ollama, OllamaError
from .models import Session
from .store import db, expiry, indexer, rag


def _client(cfg: config.Config) -> Ollama:
    return Ollama(cfg.ollama_url, cfg.chat_model, cfg.embed_model)


def _summarizer(cfg: config.Config, choice: str, embedder: Ollama):
    """Pick the backend that writes recaps. Embeddings always stay local."""
    if choice == "claude":
        client = ClaudeCLI(model=cfg.claude_model)
        problems = client.health()
        if problems:
            for problem in problems:
                print(f"warning: {problem}; falling back to {cfg.chat_model}",
                      file=sys.stderr)
            return embedder, cfg.chat_model
        return client, cfg.claude_model
    return embedder, cfg.chat_model


def _require_ollama(client: Ollama) -> bool:
    problems = client.health()
    if problems:
        print("Ollama is not ready:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return False
    return True


def _parse_since(value: str | None) -> int | None:
    """Accept `30d`, `2w`, `6h` or a bare number of days."""
    if not value:
        return None
    match = re.fullmatch(r"(\d+)\s*([dwh]?)", value.strip().lower())
    if not match:
        raise SystemExit(f"could not read --since {value!r}; try 30d, 2w or 12h")
    amount, unit = int(match.group(1)), match.group(2)
    return {"": amount, "d": amount, "w": amount * 7, "h": max(1, amount // 24)}[unit]


def _sessions_json(sessions: list[Session]) -> str:
    return json.dumps([s.to_dict() for s in sessions], indent=2, default=str)


def cmd_recap(args, cfg: config.Config) -> int:
    started = time.time()
    chosen = [args.source] if args.source else cfg.sources
    verbose = args.verbose

    if verbose:
        print("collecting sessions...", file=sys.stderr)
    found = sources.collect_all(chosen, args.days, verbose=verbose)

    if args.group == "project":
        all_groups = grouping.group(found)
        groups = grouping.rank(all_groups, args.limit)
        # Summarize and index only the sessions that are actually displayed.
        ranked = [s for g in groups for s in g.sessions]
    else:
        all_groups = None
        ranked = sources.rank(found, args.limit)
        groups = grouping.rank(grouping.group(ranked))

    if args.json:
        print(_sessions_json(ranked))
        return 0

    if not ranked:
        print(f"No AI sessions found in the last {args.days} day(s).")
        print("Try a wider window, e.g. agent-recap --days 30")
        return 0

    conn = db.connect(cfg.db_path)
    stats = None

    if args.no_llm:
        for session in ranked:
            summarize.deterministic(session)
    else:
        client = _client(cfg)
        if not _require_ollama(client):
            print("\nFalling back to summaries built from parsed fields only.\n", file=sys.stderr)
            for session in ranked:
                summarize.deterministic(session)
        else:
            stale = summarize.load_cached(conn, ranked)
            cached = len(ranked) - len(stale)
            if verbose and cached:
                print(f"  reusing {cached} cached recap(s)", file=sys.stderr)
            if stale:
                writer, writer_name = _summarizer(cfg, args.summarizer, client)
                if verbose:
                    print(f"summarizing {len(stale)} session(s) with {writer_name}...",
                          file=sys.stderr)
                summarize.run(stale, writer, batch_size=cfg.batch_size,
                              excerpt_chars=cfg.excerpt_chars, verbose=verbose)
                summarize.save(conn, stale, writer_name)
            try:
                if verbose:
                    print("indexing context...", file=sys.stderr)
                indexer.run(conn, ranked, client, verbose=verbose)
                expiry.prune(conn, cfg.max_age_days)
            except OllamaError as exc:
                print(f"warning: indexing skipped: {exc}", file=sys.stderr)
            stats = db.stats(conn)

    html = render.render(
        groups,
        store_stats=stats,
        total_projects=len(all_groups) if all_groups is not None else None,
        total_sessions=len(found),
    )
    out = Path(args.out) if args.out else cfg.html_path
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")

    blocked = [s for s in ranked if s.blocked]
    live = sum(1 for s in ranked if s.live)
    open_work = sum(1 for g in groups if g.open_todos)
    headline = f"{len(groups)} project(s) · {len(ranked)} session(s) · {live} running"
    if blocked:
        headline += f" · {len(blocked)} WAITING ON YOU"
    print(f"{headline} · {open_work} with open todos · {time.time() - started:.1f}s")
    for session in blocked:
        held = f" for {session.state_minutes}m" if session.state_minutes else ""
        print(f"  waiting: {session.project_name} — {session.waiting_for or 'input'}{held}")
    print(str(out))
    if not args.no_open:
        subprocess.run(["open", str(out)], check=False)
    return 0


def cmd_ask(args, cfg: config.Config) -> int:
    conn = db.connect(cfg.db_path)
    if db.stats(conn)["chunks"] == 0:
        print("Nothing indexed yet. Run `agent-recap index` first.", file=sys.stderr)
        return 1

    client = _client(cfg)
    if not _require_ollama(client):
        return 1

    try:
        result = rag.ask(
            conn, args.question, client,
            k=args.k, project=args.project, source=args.source,
            since_days=_parse_since(args.since),
        )
    except OllamaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0

    if not result["hits"]:
        print("No stored context matched that question.")
        return 0

    print()
    print(result["answer"] or "(no answer)")
    print()
    print("Sources:")
    for label, hit in result["labels"].items():
        project = hit["project_path"] or "unknown project"
        when = (hit["last_active"] or "")[:10]
        title = hit["title"] or hit["kind"]
        print(f"  [{label}] {project}  ({hit['source']}, {when})  {title}")
    return 0


def cmd_index(args, cfg: config.Config) -> int:
    client = _client(cfg)
    if not _require_ollama(client):
        return 1
    conn = db.connect(cfg.db_path)
    found = sources.collect_all(cfg.sources, args.days, verbose=args.verbose)
    if not found:
        print(f"No sessions found in the last {args.days} day(s).")
        return 0
    result = indexer.run(conn, found, client, force=args.reindex, verbose=args.verbose)
    verdict = expiry.prune(conn, cfg.max_age_days)
    print(
        f"indexed {result['sessions']} session(s), re-embedded {result['reindexed']} "
        f"({result['chunks']} chunks), pruned {len(verdict['drop'])}"
    )
    return 0


def cmd_prune(args, cfg: config.Config) -> int:
    conn = db.connect(cfg.db_path)
    verdict = expiry.prune(conn, args.max_age or cfg.max_age_days, dry_run=args.dry_run)
    verb = "would drop" if args.dry_run else "dropped"
    print(f"keeping {len(verdict['keep'])}, {verb} {len(verdict['drop'])}")
    for session_id, reason in verdict["drop"]:
        print(f"  - {session_id[:12]}  {reason}")
    if args.verbose:
        for session_id, reason in verdict["keep"]:
            print(f"  keep {session_id[:12]}  {reason}")
    return 0


def cmd_pin(args, cfg: config.Config) -> int:
    conn = db.connect(cfg.db_path)
    if expiry.set_pinned(conn, args.session_id, True):
        print(f"pinned {args.session_id} — it will never be pruned")
        return 0
    print(f"no stored session with id {args.session_id!r}", file=sys.stderr)
    return 1


def cmd_forget(args, cfg: config.Config) -> int:
    conn = db.connect(cfg.db_path)
    if args.project:
        rows = conn.execute(
            "SELECT session_id FROM sessions WHERE project_path LIKE ?",
            (f"%{args.project}%",),
        ).fetchall()
        ids = [r["session_id"] for r in rows]
    else:
        ids = [args.session_id] if args.session_id else []
    if not ids:
        print("nothing matched", file=sys.stderr)
        return 1
    print(f"forgot {expiry.forget(conn, ids)} session(s)")
    return 0


def cmd_status(args, cfg: config.Config) -> int:
    found = sources.collect_all(cfg.sources, args.days)
    live = [s for s in found if s.live]
    conn = db.connect(cfg.db_path)
    stats = db.stats(conn)

    print(f"Sessions in the last {args.days} day(s): {len(found)}")
    blocked = [s for s in found if s.blocked]
    for session in sources.rank(blocked):
        held = f"{session.state_minutes}m" if session.state_minutes is not None else "?"
        print(f"  WAITING  {session.project_name:28} "
              f"{session.waiting_for or 'input'} ({held})")
    for session in sources.rank([s for s in live if not s.blocked]):
        held = f" ({session.state_minutes}m)" if session.state_minutes is not None else ""
        print(f"  running  {session.project_name:28} "
              f"{session.live_state or 'live'}{held}")
    open_work = [s for s in found if s.open_todos]
    for session in sources.rank(open_work)[:10]:
        print(f"  {len(session.open_todos)} open   {session.project_name:28} "
              f"{session.open_todos[0].text[:44]}")
    print()
    print(f"Memory store: {cfg.db_path}")
    print(f"  {stats['sessions']} sessions · {stats['chunks']} chunks · "
          f"{stats['vectors']} vectors · {stats['pinned']} pinned")
    print(f"  oldest retained: {str(stats['oldest'])[:10]}")
    return 0


def cmd_doctor(args, cfg: config.Config) -> int:
    ok = True

    print(f"Ollama at {cfg.ollama_url}")
    client = _client(cfg)
    problems = client.health()
    if problems:
        ok = False
        for problem in problems:
            print(f"  FAIL  {problem}")
    else:
        print(f"  ok    reachable, {cfg.chat_model} and {cfg.embed_model} installed")

    if not problems:
        try:
            vector = client.embed(["dimension probe"])[0]
            if len(vector) == config.EMBED_DIM:
                print(f"  ok    embedding dimension {len(vector)} matches the store schema")
            else:
                ok = False
                print(f"  FAIL  {cfg.embed_model} returns {len(vector)} dims, "
                      f"store expects {config.EMBED_DIM}")
        except OllamaError as exc:
            ok = False
            print(f"  FAIL  embedding call failed: {exc}")

    print("Store")
    try:
        conn = db.connect(cfg.db_path)
        stats = db.stats(conn)
        print(f"  ok    sqlite-vec loaded, {cfg.db_path}")
        print(f"  ok    {stats['sessions']} sessions, {stats['chunks']} chunks, "
              f"{stats['vectors']} vectors")
        if stats["chunks"] != stats["vectors"]:
            print(f"  WARN  {stats['chunks']} chunks but {stats['vectors']} vectors "
                  "— run `agent-recap index --reindex`")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"  FAIL  cannot open store: {exc}")

    print("Sources")
    for name, probe in sources.PROBES.items():
        try:
            report = probe()
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"  FAIL  {name}: probe crashed: {exc}")
            continue
        if not report["present"]:
            print(f"  none  {name}: {report['detail']}")
        elif report["healthy"]:
            print(f"  ok    {name}: {report['detail']}")
        else:
            ok = False
            print(f"  FAIL  {name}: {report['detail']}")

    print("Schedule")
    info = schedule.status()
    if not info["installed"]:
        print("  none  no daily schedule (agent-recap schedule --at 08:30)")
    else:
        state = "loaded" if info["loaded"] else "NOT loaded"
        if not info["loaded"]:
            ok = False
            print(f"  FAIL  daily run at {info.get('at', '?')} is {state}")
        else:
            print(f"  ok    daily run at {info.get('at', '?')} ({state})")
        if info.get("target") and not info.get("target_exists"):
            ok = False
            print(f"  FAIL  scheduled binary is missing: {info['target']}")
            print("        reinstall, then re-run `agent-recap schedule --at HH:MM`")

    print("\n" + ("all good" if ok else "problems found — see FAIL lines above"))
    return 0 if ok else 1


SUBCOMMANDS = frozenset(
    {"recap", "ask", "index", "prune", "pin", "forget", "status", "schedule", "doctor"}
)


def cmd_schedule(args, cfg: config.Config) -> int:
    if args.uninstall:
        removed = schedule.uninstall()
        print("removed the daily schedule" if removed else "no schedule was installed")
        return 0

    if not args.at:
        info = schedule.status()
        if not info["installed"]:
            print("No schedule installed. Add one with:")
            print("  agent-recap schedule --at 08:30")
            return 0
        print(f"Daily run at {info.get('at', '?')} "
              f"({'loaded' if info['loaded'] else 'NOT loaded'})")
        print(f"  {info.get('command', '')}")
        if info.get("last_run"):
            stamp = time.strftime("%a %d %b %H:%M", time.localtime(info["last_run"]))
            print(f"  last wrote output: {stamp}  ({info['log']})")
        return 0

    try:
        hour, _, minute = args.at.partition(":")
        hour, minute = int(hour), int(minute or 0)
        if not (0 <= hour < 24 and 0 <= minute < 60):
            raise ValueError
    except ValueError:
        print(f"could not read --at {args.at!r}; use HH:MM, e.g. 08:30", file=sys.stderr)
        return 1

    try:
        path = schedule.install(hour=hour, minute=minute, days=args.days,
                                limit=args.limit, summarizer=args.summarizer)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"Scheduled a daily recap at {hour:02d}:{minute:02d}")
    print(f"  {path}")
    print(f"  logs: {cfg.db_path.parent / 'schedule.log'}")
    print("It writes the page without opening it; run `agent-recap` to view.")
    return 0


def build_parser(cfg: config.Config) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-recap",
        description="Recap and search your local AI coding sessions, fully offline.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")

    # Shared so `-v` works both before and after the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-v", "--verbose", action="store_true")

    sub = parser.add_subparsers(dest="command", parser_class=argparse.ArgumentParser)

    def new_sub(name, **kw):
        return sub.add_parser(name, parents=[common], **kw)

    def add_recap(p):
        p.add_argument("--days", type=int, default=cfg.days,
                       help=f"how far back to look (default {cfg.days})")
        p.add_argument("--limit", type=int, default=cfg.limit,
                       help=f"max cards (default {cfg.limit})")
        p.add_argument("--source", choices=sorted(sources.COLLECTORS))
        p.add_argument("--no-llm", action="store_true",
                       help="skip the model; summarize from parsed fields only")
        p.add_argument("--no-open", action="store_true", help="do not open the browser")
        p.add_argument("--out", help="where to write the HTML")
        p.add_argument("--json", action="store_true", help="dump sessions instead of rendering")
        p.add_argument("--group", choices=("project", "session"), default="project",
                       help="one card per project (default) or per session")
        p.add_argument("--summarizer", choices=("ollama", "claude"), default="ollama",
                       help="ollama (free, offline) or the claude CLI (sharper, costs tokens)")
        p.set_defaults(func=cmd_recap)

    add_recap(new_sub("recap", help="build the HTML recap (default)"))

    p = new_sub("ask", help="ask a question about your past sessions")
    p.add_argument("question")
    p.add_argument("--k", type=int, default=8, help="context chunks to retrieve")
    p.add_argument("--project", help="restrict to project paths containing this")
    p.add_argument("--source", choices=sorted(sources.COLLECTORS))
    p.add_argument("--since", help="only context newer than e.g. 30d, 2w")
    p.add_argument("--json", action="store_true", help="dump hits and scores")
    p.set_defaults(func=cmd_ask)

    p = new_sub("index", help="collect and embed, without rendering")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--reindex", action="store_true", help="re-embed everything")
    p.set_defaults(func=cmd_index)

    p = new_sub("prune", help="expire stale context")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--max-age", type=int)
    p.set_defaults(func=cmd_prune)

    p = new_sub("pin", help="never expire this session's context")
    p.add_argument("session_id")
    p.set_defaults(func=cmd_pin)

    p = new_sub("forget", help="delete stored context")
    p.add_argument("session_id", nargs="?")
    p.add_argument("--project")
    p.set_defaults(func=cmd_forget)

    p = new_sub("status", help="live sessions and store stats")
    p.add_argument("--days", type=int, default=cfg.days)
    p.set_defaults(func=cmd_status)

    p = new_sub("schedule", help="run the recap automatically each morning")
    p.add_argument("--at", help="local time to run, HH:MM (omit to show status)")
    p.add_argument("--uninstall", action="store_true")
    p.add_argument("--days", type=int, default=cfg.days)
    p.add_argument("--limit", type=int, default=cfg.limit)
    p.add_argument("--summarizer", choices=("ollama", "claude"), default="ollama")
    p.set_defaults(func=cmd_schedule)

    p = new_sub("doctor", help="check models, store and sources")
    p.set_defaults(func=cmd_doctor)

    return parser


def main(argv: list[str] | None = None) -> int:
    cfg = config.load()
    config.ensure_data_dir()
    parser = build_parser(cfg)

    argv = list(sys.argv[1:] if argv is None else argv)
    # Bare `agent-recap`, or one starting with a flag, means `recap`.
    if not argv or argv[0] not in SUBCOMMANDS:
        if not argv or argv[0] not in ("-h", "--help"):
            argv = ["recap", *argv]

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 1
    try:
        return args.func(args, cfg)
    except KeyboardInterrupt:
        return 130
