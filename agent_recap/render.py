"""Render the recap as one self-contained HTML file.

The design is a status board for agents working across repositories: colour is
reserved entirely for state (waiting / running / unfinished), monospace carries
machine facts, and a proportional face carries the written recap. Tool identity
is typographic rather than coloured, so nothing competes with urgency.

No template engine and no external assets, so the output opens straight from
disk.
"""

from __future__ import annotations

import html
import os
from datetime import datetime
from string import Template

from .grouping import ProjectGroup
from .models import Session, now
from .scrub import scrub

MAX_WORK_SHOWN = 6

SOURCE_LABEL = {"claude-code": "claude code", "cursor": "cursor", "vscode": "vs code"}

_CSS = """
:root{
  --paper:#f7f8fa; --panel:#fff; --panel-2:#f2f4f7;
  --rule:#dde2e8;
  --ink:#101720; --ink-2:#57636f; --ink-3:#8b97a3;
  --wait:#b3261e; --wait-bg:#fdf3f2; --wait-rule:#e8b4b0;
  --run:#0b6e4f; --run-bg:#f1f8f4;
  --open:#7a5200; --open-bg:#fbf6ea;
  --mono:ui-monospace,"SF Mono",SFMono-Regular,"JetBrains Mono",Menlo,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Inter","Segoe UI",system-ui,sans-serif;
}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;color:var(--ink);background:var(--paper);font:15px/1.6 var(--sans)}
.wrap{max-width:1060px;margin:0 auto;padding:40px 24px 72px}

/* ---- masthead ---- */
.mast{display:flex;flex-wrap:wrap;align-items:flex-end;gap:16px;
  border-bottom:2px solid var(--ink);padding-bottom:10px;margin-bottom:0}
h1{margin:0;font:600 20px/1.2 var(--sans);letter-spacing:-.015em}
.clock{margin-left:auto;font:400 12px/1 var(--mono);color:var(--ink-2);
  font-variant-numeric:tabular-nums}
.tally{display:flex;flex-wrap:wrap;gap:0;margin:0 0 26px;
  border:1px solid var(--rule);border-top:0;background:var(--panel)}
.tally div{padding:9px 14px;border-right:1px solid var(--rule);
  font:400 12px/1.35 var(--mono);color:var(--ink-2)}
.tally div:last-child{border-right:0}
.tally b{display:block;font:600 17px/1.2 var(--mono);color:var(--ink);
  font-variant-numeric:tabular-nums}
.tally div.hot b{color:var(--wait)}

/* ---- the one loud element: what is waiting on you ---- */
.alert{border:1px solid var(--wait-rule);border-left:4px solid var(--wait);
  background:var(--wait-bg);padding:14px 18px;margin:0 0 24px;border-radius:3px}
.alert h2{margin:0 0 8px;font:600 13px/1 var(--mono);color:var(--wait)}
.alert ul{margin:0;padding:0;list-style:none;display:grid;gap:5px}
.alert li{font:400 13.5px/1.5 var(--sans);color:var(--ink)}
.alert .who{font:600 13px/1 var(--mono)}
.alert .held{font:400 12.5px/1 var(--mono);color:var(--wait);
  font-variant-numeric:tabular-nums}
.allclear{border:1px solid var(--rule);border-left:4px solid var(--run);
  background:var(--run-bg);padding:12px 18px;margin:0 0 24px;border-radius:3px;
  font:400 13.5px/1.5 var(--sans);color:var(--ink-2)}
.allclear b{font:600 13px/1 var(--mono);color:var(--run)}

/* ---- filters ---- */
.filters{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:18px}
.filters button{
  font:400 12px/1 var(--mono);color:var(--ink-2);background:var(--panel);
  border:1px solid var(--rule);border-radius:3px;padding:6px 11px;cursor:pointer}
.filters button:hover{border-color:var(--ink-3);color:var(--ink)}
.filters button:focus-visible{outline:2px solid var(--ink);outline-offset:2px}
.filters button[aria-pressed=true]{background:var(--ink);border-color:var(--ink);
  color:#fff}

/* ---- project rows ---- */
.board{display:grid;gap:10px}
.row{display:grid;grid-template-columns:104px 1fr;
  background:var(--panel);border:1px solid var(--rule);
  border-left:3px solid var(--ink-3);border-radius:3px}
.row[data-state=waiting]{border-left-color:var(--wait);border-color:var(--wait-rule)}
.row[data-state=running]{border-left-color:var(--run)}
.row[data-state=unfinished]{border-left-color:var(--open)}
.row.hidden{display:none}

.gutter{padding:14px 12px;border-right:1px solid var(--rule);
  font:400 11.5px/1.45 var(--mono);color:var(--ink-2)}
.gutter .state{display:block;color:var(--ink)}
.gutter .elapsed{display:block;color:var(--ink-3);
  font-variant-numeric:tabular-nums}
.row[data-state=waiting] .gutter .state{color:var(--wait);font-weight:600}
.row[data-state=running] .gutter .state{color:var(--run)}
.dot{display:inline-block;width:6px;height:6px;border-radius:50%;
  background:currentColor;margin-right:5px;vertical-align:1px}
.row[data-state=waiting] .dot{animation:blink 1.6s steps(2,end) infinite}
@keyframes blink{50%{opacity:.2}}

.body{padding:13px 16px 14px;min-width:0}
.head{display:flex;flex-wrap:wrap;align-items:baseline;gap:9px}
.name{font:600 16px/1.25 var(--sans);letter-spacing:-.01em}
.tool{font:400 11px/1 var(--mono);color:var(--ink-2);background:var(--panel-2);
  border:1px solid var(--rule);border-radius:2px;padding:3px 6px}
.path{margin:3px 0 0;font:400 11.5px/1.5 var(--mono);color:var(--ink-3);
  word-break:break-all}
.subject{margin:8px 0 0;font:400 13px/1.5 var(--sans);color:var(--ink-2)}
.recap{margin:7px 0 0;max-width:72ch}
.next{margin:11px 0 0;display:grid;grid-template-columns:auto 1fr;gap:10px;
  align-items:baseline;background:var(--open-bg);border-radius:3px;padding:8px 11px}
.next dt{font:600 11px/1.5 var(--mono);color:var(--open)}
.next dd{margin:0;font:400 13.5px/1.5 var(--sans)}

/* key=value machine facts, in the vein of process output */
.facts{margin:12px 0 0;display:flex;flex-wrap:wrap;gap:4px 14px;
  font:400 11.5px/1.6 var(--mono);color:var(--ink-3);
  border-top:1px solid var(--rule);padding-top:9px}
.facts span b{font-weight:400;color:var(--ink-2)}

/* ---- work items ---- */
.worklabel{margin:13px 0 6px;font:400 11px/1 var(--mono);color:var(--ink-3);
  display:flex;gap:8px;flex-wrap:wrap;align-items:baseline}
.worklabel .doc{color:#a3adb8}
.work{list-style:none;margin:0;padding:0;display:grid;gap:3px}
.work li{display:grid;grid-template-columns:78px 1fr;gap:10px;
  font:400 13.5px/1.5 var(--sans);align-items:baseline}
.work .flag{font:400 10.5px/1.7 var(--mono);color:var(--ink-3);
  border:1px solid var(--rule);border-radius:2px;text-align:center;
  background:var(--panel-2)}
.work li.in_progress .flag{color:var(--open);border-color:#e0d0a8;
  background:var(--open-bg)}
.work li.completed .flag{color:var(--ink-3)}
.work li.completed .text{color:var(--ink-3);text-decoration:line-through}
.work li.unverified .flag{border-style:dashed;color:var(--ink-3)}

/* ---- disclosures ---- */
details{margin-top:12px}
summary{cursor:pointer;font:400 11.5px/1 var(--mono);color:var(--ink-3);
  list-style:none;padding:3px 0}
summary::-webkit-details-marker{display:none}
summary:hover{color:var(--ink-2)}
summary:focus-visible{outline:2px solid var(--ink);outline-offset:2px}
summary::before{content:"[+] "}
details[open] summary::before{content:"[-] "}
.sub{display:grid;gap:10px;margin-top:8px;padding-left:14px;
  border-left:1px solid var(--rule)}
.sub .shead{display:flex;flex-wrap:wrap;gap:8px;align-items:baseline;
  font:400 11.5px/1.5 var(--mono);color:var(--ink-3)}
.sub .srecap{font:400 13px/1.5 var(--sans);color:var(--ink-2);max-width:72ch}
.trimmed{font:400 11.5px/1.5 var(--mono);color:var(--ink-3)}
.more{margin:4px 0 0;font:400 11.5px/1.5 var(--mono);color:var(--ink-3)}
.tail{margin-top:8px;background:var(--panel-2);border:1px solid var(--rule);
  border-radius:3px;padding:10px 12px;white-space:pre-wrap;overflow:auto;
  max-height:280px;font:400 12px/1.6 var(--mono);color:var(--ink-2)}
.tail .who{display:block;margin-bottom:5px;color:var(--ink-3)}

.empty{background:var(--panel);border:1px solid var(--rule);border-radius:3px;
  padding:32px;text-align:center;color:var(--ink-2)}
.empty code,.foot code{font:400 12.5px/1 var(--mono);background:var(--panel-2);
  border:1px solid var(--rule);border-radius:2px;padding:2px 5px}
.foot{margin-top:28px;padding-top:12px;border-top:1px solid var(--rule);
  font:400 11.5px/1.7 var(--mono);color:var(--ink-3);
  display:flex;flex-wrap:wrap;gap:4px 16px}

@media (max-width:640px){
  .wrap{padding:24px 16px 56px}
  .row{grid-template-columns:1fr}
  .gutter{border-right:0;border-bottom:1px solid var(--rule);
    display:flex;gap:10px;padding:10px 14px}
  .work li{grid-template-columns:1fr;gap:1px}
  .work .flag{justify-self:start;padding:0 6px}
}
@media (prefers-reduced-motion:reduce){*{animation:none!important}}
"""

_JS = """
const buttons=[...document.querySelectorAll('.filters button')];
buttons.forEach(b=>b.addEventListener('click',()=>{
  buttons.forEach(o=>o.setAttribute('aria-pressed',String(o===b)));
  const want=b.dataset.filter;
  document.querySelectorAll('.row').forEach(row=>{
    const show = want==='all'
      || row.dataset.state===want
      || (row.dataset.tools||'').split(' ').includes(want)
      || (want==='open' && row.dataset.open==='1');
    row.classList.toggle('hidden',!show);
  });
}));
"""

_PAGE = Template(
    """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>agent-recap</title><style>$css</style></head>
<body><div class="wrap">
<div class="mast"><h1>$title</h1><span class="clock">$stamp</span></div>
<div class="tally">$tally</div>
$alert
<div class="filters">$filters</div>
<div class="board">$rows</div>
<div class="foot">$foot</div>
</div><script>$js</script></body></html>
"""
)


def _esc(text) -> str:
    return html.escape(str(text)) if text is not None else ""


def _tilde(path: str | None) -> str:
    if not path:
        return ""
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path.startswith(home) else path


def _relative(when: datetime) -> str:
    seconds = (now() - when).total_seconds()
    if seconds < 90:
        return "now"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h"
    days = hours // 24
    return f"{days}d" if days < 14 else when.strftime("%-d %b")


def _duration(minutes: int | None) -> str:
    if minutes is None:
        return ""
    if minutes < 60:
        return f"{minutes}m"
    if minutes < 1440:
        return f"{minutes // 60}h"
    return f"{minutes // 1440}d"


def _state_of(group: ProjectGroup) -> str:
    if group.blocked:
        return "waiting"
    if group.live:
        return "running"
    if group.open_todos or group.ended_mid_task:
        return "unfinished"
    return "idle"


def _gutter(group: ProjectGroup) -> str:
    """State and elapsed time, in a fixed column so states scan vertically."""
    state = _state_of(group)
    if state == "waiting":
        session = group.blocked_sessions[0]
        word, held = "waiting", _duration(session.state_minutes)
    elif state == "running":
        session = next(s for s in group.sessions if s.live)
        word = session.live_state or "running"
        held = _duration(session.state_minutes)
    else:
        word = "unfinished" if state == "unfinished" else "quiet"
        held = _relative(group.last_active_at)
    dot = '<span class="dot"></span>' if state in ("waiting", "running") else ""
    return (
        f'<div class="gutter"><span class="state">{dot}{_esc(word)}</span>'
        f'<span class="elapsed">{_esc(held)}</span></div>'
    )


def _facts(group: ProjectGroup) -> str:
    """Machine facts as key=value pairs, the way process output reads."""
    pairs: list[tuple[str, str]] = []
    git = group.git
    if git:
        if git.branch:
            pairs.append(("branch", git.branch))
        elif git.note:
            pairs.append(("git", git.note.replace(" ", "-")))
        if git.dirty_files:
            pairs.append(("uncommitted", str(git.dirty_files)))
        if git.ahead:
            pairs.append(("unpushed", str(git.ahead)))
    messages = sum(s.message_count or 0 for s in group.sessions)
    if messages:
        partial = any(s.extras.get("counts_are_partial") for s in group.sessions)
        pairs.append(("messages", f"{messages}{'+' if partial else ''}"))
    total = len(group.sessions) + group.trimmed
    if total > 1:
        pairs.append(("sessions", str(total)))
    if group.lead.model:
        pairs.append(("model", group.lead.model))
    if not pairs:
        return ""
    return '<div class="facts">' + "".join(
        f"<span><b>{_esc(k)}</b>={_esc(v)}</span>" for k, v in pairs
    ) + "</div>"


def _work_items(todos) -> str:
    if not todos:
        return ""
    parts = ['<ul class="work">']
    for todo in todos:
        classes = [todo.status]
        if not todo.verified:
            classes.append("unverified")
        flag = "unverified" if not todo.verified else todo.status.replace("_", " ")
        parts.append(
            f'<li class="{" ".join(classes)}"><span class="flag">{_esc(flag)}</span>'
            f'<span class="text">{_esc(todo.text)}</span></li>'
        )
    parts.append("</ul>")
    return "".join(parts)


def _work(open_todos, done_todos, *, plan_name: str | None = None) -> str:
    """Reported todos and inferred plan steps, kept visually separate.

    An agent's own list states its status; a plan step's status is deduced, or
    sometimes unknown. Merging them would present a guess as a fact.
    """
    everything = list(open_todos) + list(done_todos)
    reported = [t for t in everything if not t.inferred]
    inferred = [t for t in everything if t.inferred]

    parts = []
    if reported:
        open_items = [t for t in reported if t.is_open]
        done = [t for t in reported if not t.is_open]
        note = "tracked by the agent"
        if done:
            note += f", {len(done)}/{len(reported)} done"
        parts.append(f'<div class="worklabel"><span>{_esc(note)}</span></div>')
        parts.append(_work_items(open_items[:MAX_WORK_SHOWN]))
        hidden = max(0, len(open_items) - MAX_WORK_SHOWN)
        if hidden:
            parts.append(f'<p class="more">+{hidden} more open</p>')
    if inferred:
        open_steps = [t for t in inferred if t.is_open]
        done = len(inferred) - len(open_steps)
        note = f"plan steps, {done}/{len(inferred)} done" if done else "plan steps"
        doc = f'<span class="doc">{_esc(plan_name)}</span>' if plan_name else ""
        parts.append(f'<div class="worklabel"><span>{_esc(note)}</span>{doc}</div>')
        parts.append(_work_items(open_steps[:MAX_WORK_SHOWN]))
        hidden = max(0, len(open_steps) - MAX_WORK_SHOWN)
        if hidden:
            parts.append(f'<p class="more">+{hidden} more step(s)</p>')
    return "".join(parts)


def _tail(session: Session) -> str:
    pairs = [("you said", session.last_user_text),
             ("the agent replied", session.last_assistant_text)]
    if not any(text for _, text in pairs):
        return ""
    parts = ["<details><summary>conversation tail</summary>"]
    for label, text in pairs:
        if not text:
            continue
        clipped = scrub(text) or ""
        if len(clipped) > 2600:
            clipped = clipped[:2600] + "\n…"
        parts.append(
            f'<div class="tail"><span class="who">{label}</span>{_esc(clipped)}</div>'
        )
    parts.append("</details>")
    return "".join(parts)


def _normalize(text: str) -> str:
    return "".join(c for c in text.lower() if c.isalnum())


def _duplicates_a_todo(next_step: str | None, group: ProjectGroup) -> bool:
    """True when the suggested next step just restates an open work item.

    The work item is the better presentation of the two, because it carries a
    status flag.
    """
    if not next_step:
        return False
    target = _normalize(next_step)
    if not target:
        return False
    for todo in group.open_todos:
        other = _normalize(todo.text)
        if not other:
            continue
        if target == other or target in other or other in target:
            return True
    return False


def _row(group: ProjectGroup) -> str:
    lead = group.lead
    state = _state_of(group)
    parts = [
        f'<article class="row" data-state="{state}" '
        f'data-tools="{_esc(" ".join(group.sources))}" '
        f'data-open="{1 if group.open_todos else 0}">'
    ]
    parts.append(_gutter(group))
    parts.append('<div class="body">')

    parts.append('<div class="head">')
    parts.append(f'<span class="name">{_esc(group.name)}</span>')
    for source in group.sources:
        count = sum(1 for s in group.sessions if s.source == source)
        label = SOURCE_LABEL.get(source, source) + (f" ×{count}" if count > 1 else "")
        parts.append(f'<span class="tool">{_esc(label)}</span>')
    parts.append("</div>")

    if group.project_path:
        parts.append(f'<p class="path">{_esc(_tilde(group.project_path))}</p>')
    if lead.title:
        parts.append(f'<p class="subject">{_esc(lead.title)}</p>')
    if lead.recap:
        parts.append(f'<p class="recap">{_esc(lead.recap)}</p>')
    if lead.next_step and not _duplicates_a_todo(lead.next_step, group):
        parts.append(
            '<dl class="next"><dt>pick up here</dt>'
            f"<dd>{_esc(lead.next_step)}</dd></dl>"
        )

    parts.append(_facts(group))
    parts.append(_work(group.open_todos, group.done_todos, plan_name=group.plan_name))

    others = group.sessions[1:]
    if others or group.trimmed:
        count = len(others) + group.trimmed
        parts.append(
            f"<details><summary>{count} more session"
            f'{"s" if count > 1 else ""} here</summary><div class="sub">'
        )
        for session in others:
            parts.append('<div>')
            parts.append('<div class="shead">')
            parts.append(f"<span>{_esc(SOURCE_LABEL.get(session.source, session.source))}</span>")
            if session.blocked:
                parts.append(f'<span style="color:var(--wait)">waiting '
                             f"{_esc(_duration(session.state_minutes))}</span>")
            elif session.live:
                parts.append(f"<span>{_esc(session.live_state or 'live')}</span>")
            parts.append(f"<span>{_esc(_relative(session.last_active_at))}</span>")
            if session.title:
                parts.append(f"<span>{_esc(session.title)}</span>")
            parts.append("</div>")
            if session.recap:
                parts.append(f'<p class="srecap">{_esc(session.recap)}</p>')
            parts.append(_work(session.open_todos, [], plan_name=session.plan_name))
            parts.append("</div>")
        if group.trimmed:
            parts.append(
                f'<p class="trimmed">{group.trimmed} older session'
                f'{"s" if group.trimmed > 1 else ""} not shown</p>'
            )
        parts.append("</div></details>")

    parts.append(_tail(lead))
    parts.append("</div></article>")
    return "".join(parts)


def _alert(groups: list[ProjectGroup]) -> str:
    blocked = [g for g in groups if g.blocked]
    if not blocked:
        unfinished = sum(1 for g in groups if g.open_todos)
        if not unfinished:
            return '<p class="allclear"><b>all clear</b> Nothing is waiting on you.</p>'
        return (
            '<p class="allclear"><b>all clear</b> Nothing is waiting on you. '
            f"{unfinished} project{'s' if unfinished > 1 else ''} still "
            "carry unfinished work below.</p>"
        )
    items = []
    for group in blocked:
        for session in group.blocked_sessions:
            held = _duration(session.state_minutes)
            items.append(
                f'<li><span class="who">{_esc(group.name)}</span> needs '
                f"{_esc(session.waiting_for or 'your input')}"
                + (f' <span class="held">{_esc(held)} so far</span>' if held else "")
                + "</li>"
            )
    return ('<div class="alert"><h2>waiting on you</h2><ul>'
            + "".join(items) + "</ul></div>")


def render(groups: list[ProjectGroup], *, store_stats: dict | None = None,
           total_projects: int | None = None, total_sessions: int | None = None) -> str:
    sessions = sum(len(g.sessions) + g.trimmed for g in groups)
    waiting = sum(1 for g in groups if g.blocked)
    running = sum(1 for g in groups if g.live)
    unfinished = sum(1 for g in groups if g.open_todos)

    tally = [
        ("projects", str(len(groups)), False),
        ("sessions", str(sessions), False),
        ("waiting on you", str(waiting), waiting > 0),
        ("running", str(running), False),
        ("unfinished work", str(unfinished), False),
    ]
    tally_html = "".join(
        f'<div{" class=\'hot\'" if hot else ""}><b>{_esc(value)}</b>{_esc(label)}</div>'
        for label, value, hot in tally
    )

    filters = [("all", f"all {len(groups)}")]
    if waiting:
        filters.append(("waiting", f"waiting {waiting}"))
    if running:
        filters.append(("running", f"running {running}"))
    if unfinished:
        filters.append(("open", f"unfinished {unfinished}"))
    by_tool: dict[str, int] = {}
    for group in groups:
        for source in group.sources:
            by_tool[source] = by_tool.get(source, 0) + 1
    for source, count in sorted(by_tool.items()):
        filters.append((source, f"{SOURCE_LABEL.get(source, source)} {count}"))
    filters_html = "".join(
        f'<button data-filter="{_esc(key)}" '
        f'aria-pressed="{"true" if key == "all" else "false"}">{_esc(label)}</button>'
        for key, label in filters
    )

    rows = "".join(_row(g) for g in groups) or (
        '<div class="empty">No AI sessions in this window. '
        "Try <code>agent-recap --days 30</code>.</div>"
    )

    foot = []
    if total_projects is not None and total_projects > len(groups):
        foot.append(f"showing {len(groups)} of {total_projects} projects")
    if total_sessions:
        foot.append(f"{total_sessions} sessions scanned")
    if store_stats:
        foot.append(f"{store_stats.get('sessions', 0)} sessions remembered")
        foot.append(f"{store_stats.get('chunks', 0)} chunks indexed")
    foot.append('search it with <code>agent-recap ask "…"</code>')

    return _PAGE.substitute(
        css=_CSS,
        js=_JS,
        title="Where you left off",
        stamp=now().astimezone().strftime("%a %-d %b · %-I:%M %p").lower(),
        tally=tally_html,
        alert=_alert(groups),
        filters=filters_html,
        rows=rows,
        foot="".join(f"<span>{item}</span>" for item in foot),
    )
