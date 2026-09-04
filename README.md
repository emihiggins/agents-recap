# agent-recap

You had six AI sessions going. Which one still has work in it?

`agent-recap` reads the session state your AI tools already keep on disk, works out
where each project actually stands, and writes one HTML page telling you where you
left off. It also remembers that context so you can ask about it later.

Everything runs locally against [Ollama](https://ollama.com). No API keys, no
network calls, nothing leaves the machine.

```
agent-recap                       # build the recap and open it
agent-recap ask "what was I doing with the rates API?"
agent-recap status                # what's running right now
agent-recap schedule --at 08:30   # have it ready before you sit down
```

![The recap page: a waiting-on-you banner, then one card per project with its
recap, suggested next step, git state and unfinished work](docs/recap.png)

*Sample data. Regenerate with `python scripts/sample_recap.py`.*

It leads with what's actually blocking you:

```
Waiting on you
  storefront-web — permission prompt, waiting 48m
```

An agent stalled on a permission prompt for the last three quarters of an hour is
the single most useful thing this can tell you, so it gets a banner.

## What it reads

| Source | Where | What it gets |
| --- | --- | --- |
| Claude Code | `~/.claude` | Live session status, titles, last messages, plan documents and their remaining steps |
| Cursor | Cursor's global `state.vscdb` | Composer/agent sessions, titles, agent todo lists, context usage |
| VS Code Copilot Chat | VS Code `workspaceStorage` | Chat sessions (both the legacy and journal formats), Copilot todo lists |
| git | each project directory | Branch, uncommitted files, unpushed commits |

All of it is opened **read-only**. Cursor and VS Code are usually running while this
executes, so connections are opened with `mode=ro` and the test suite asserts that
writes and DDL are rejected. `agent-recap` never modifies another tool's data.

## Getting started

**Requirements**

- macOS. All the storage paths this reads are macOS-specific.
- Python 3.11+ (3.13 recommended).
- [Ollama](https://ollama.com) — this is what writes the summaries and embeddings,
  so nothing leaves your machine and there is no API key.
- ~6 GB of disk for the two models, and enough RAM to run an 8B model
  (16 GB is comfortable).

**1. Install Ollama and pull the two models**

```sh
brew install ollama
brew services start ollama          # or just `ollama serve` in a terminal

ollama pull qwen3:8b                # summaries    (~5.2 GB)
ollama pull nomic-embed-text        # embeddings   (~274 MB)
```

If you'd rather have the menu-bar app, `brew install --cask ollama-app` runs the
same server.

Ollama has to be *running*, not merely installed — check with
`curl -s localhost:11434/api/tags`, or just run `agent-recap doctor` below. Both
models are required: a chat model cannot produce embeddings, so `nomic-embed-text`
is not optional.

**2. Install agent-recap**

Pick **one** installer and stay with it — see the warning below.

```sh
git clone https://github.com/emihiggins/agents-recap.git
cd agents-recap
uv tool install .                   # or: pipx install .
```

If you don't have `uv`: `brew install uv`. `pipx install .` works equally well.
Both put the `agent-recap` binary in `~/.local/bin`, so make sure that directory
is on your `PATH`:

```sh
echo $PATH | tr ':' '\n' | grep -q "$HOME/.local/bin" && echo ok || \
  echo 'add ~/.local/bin to PATH in your shell profile'
```

**3. Check everything is wired up**

```sh
agent-recap doctor
```

This is the command to run whenever anything looks wrong. It verifies Ollama is
reachable, both models are installed, the embedding dimension matches the store
schema, each source's on-disk format still looks the way the parsers expect, and
the daily schedule (if you set one up) points at a binary that exists.

**4. Run it**

```sh
agent-recap
```

First run takes about a minute: it reads your sessions, summarizes them, and
embeds the context. Later runs are much faster, because sessions whose content
hasn't changed reuse their cached summary. It writes
`~/.agent-recap/recap.html` and opens it.

Seeing nothing? The default window is the last 7 days — try `agent-recap --days 30`.
In a hurry or offline? `agent-recap --no-llm` skips the model entirely and is
near-instant.

**5. Optional: have it ready each morning**

```sh
agent-recap schedule --at 08:30
```

Read the scheduling notes below first — macOS has a specific trap here.

### Pick one installer

`uv tool install` and `pipx install` both write `~/.local/bin/agent-recap`. If you
run both, whichever ran last owns the symlink, and **uninstalling either one
deletes the binary out from under the other** — which also silently breaks a
scheduled run. `agent-recap doctor` now flags a schedule whose binary has gone
missing, but the simpler fix is to use one installer.

### Troubleshooting

`agent-recap doctor` diagnoses most of these. Every row below is a failure that
actually happened while building this.

| Symptom | Cause and fix |
| --- | --- |
| `command not found: agent-recap` | `~/.local/bin` isn't on your `PATH`. |
| `cannot reach Ollama ... Is 'ollama serve' running?` | Ollama is installed but not running. Start the app, or `brew services start ollama`. The recap still works meanwhile — it falls back to summaries built from parsed fields. |
| `model 'nomic-embed-text' not installed` | `ollama pull nomic-embed-text`. A chat model cannot serve embeddings. |
| `No AI sessions found in the last 7 day(s)` | Widen the window: `agent-recap --days 30`. |
| `Nothing indexed yet. Run 'agent-recap index' first.` | `ask` needs the store populated. `--no-llm` runs skip indexing by design. |
| Code changes don't take effect | `uv tool install --force --reinstall .`. `--force` alone reuses a cached wheel. |
| The binary vanished after uninstalling something | `uv` and `pipx` share `~/.local/bin/agent-recap`; uninstalling one deletes it for both. Reinstall, and use one installer. |
| The 08:30 run never happens | Either the scheduled binary is gone (`doctor` reports this) or its code lives somewhere macOS hides from launchd. See the scheduling notes. |
| `doctor` reports a source `FAIL` | The tool changed its storage format. The message names the field that moved. |
| Recaps read as vague | Try `--summarizer claude` for a sharper pass, at the cost of tokens and network. |

### Working on the code

```sh
uv venv && uv pip install -e . pytest
uv run pytest tests/ -q
```

To update the installed copy after editing:

```sh
uv tool install --force --reinstall .
```

`--reinstall` is required. `--force` on its own reuses a cached wheel and silently
leaves the old code in place.

Note that `uv tool install --editable` is **not** a good choice here if you use
the scheduler: it leaves the source in your checkout, which macOS may hide from
launchd. See the scheduling notes.

## Commands

| Command | What it does |
| --- | --- |
| `agent-recap` | Collect, summarize, index, prune, write HTML, open it |
| `agent-recap schedule --at 08:30` | Run it daily via launchd (`--uninstall` to remove, no args for status) |
| `agent-recap ask "..."` | Answer a question from stored context, with citations |
| `agent-recap status` | Running sessions and open todos, in the terminal |
| `agent-recap index` | Collect and embed without rendering |
| `agent-recap prune --dry-run` | Show what expiry would remove |
| `agent-recap pin <id>` | Never expire this session's context |
| `agent-recap forget <id>` / `--project X` | Delete stored context |
| `agent-recap doctor` | Health check |

Useful flags: `--days`, `--limit`, `--source`, `--no-llm` (skip the model entirely),
`--no-open`, `--json`, `-v`, plus:

- `--group session` — one card per session instead of per project.
- `--summarizer claude` — write recaps with the local `claude` CLI instead of Ollama.
  Sharper, but costs tokens and needs network. Embeddings always stay local.

## One card per project

Sessions are grouped by project, because "where did I leave off on X" is a question
about a project, not a session — and a single project is often being worked on from
two terminals and a Cursor window at once. Here that turned 38 sessions into 18
projects. Each card carries the project's merged todo list and one git state; the
other sessions on it are listed underneath, and any beyond the display cap are
reported as a count rather than dropped silently.

`--group session` gives you the old per-session view.

## How things are ordered

Most actionable first: **waiting on you**, then **running**, then **has open
todos**, then **stopped mid-task**, then most recent. The first four survive the
`--limit` cap even when older than the newest chatter, because those are the ones
you need to see.

- **Waiting on you** — Claude Code records a live status per process, so a session
  parked on a permission prompt or an input request is detected along with how long
  it's been stuck.
- **Stopped mid-task** — the last assistant turn issued a tool call with no matching
  result, or a todo was left open.

## Two kinds of open work

The card separates work an agent *reported* from work this tool *inferred*, and
never blends them.

**Reported todos** come from Cursor's agent todo list and Copilot's
`manage_todo_list`. Their status is authoritative.

**Plan steps** are for Claude Code, which has no usable todo list — its task
tools go unused in practice, and its plan documents contain no checkboxes. So
`agent-recap` extracts work items from the `Build order`, `Next steps` and
`Phase N` sections of `~/.claude/plans/<slug>.md` (joined to the session by the
`slug` it records) and judges each one **against files on disk**:

| Evidence | Shown as |
| --- | --- |
| Every file the step names exists | done |
| A file it names is missing | outstanding |
| The step names no files | outstanding, marked `unverified` |

That last row is the honest case, and it's why this isn't a model call. Given
file evidence, an 8B model adds nothing over the check itself; without it, the
model was observed marking plainly-unstarted work as finished. Since the whole
point is not forgetting things, unknown resolves to *shown*, not *hidden*.

Only steps confirmed unfinished (their files are genuinely missing) are allowed
to influence the recap text or the suggested next step. Feeding unverified
steps to the summarizer made it narrate completed work as still in progress.

Inferred steps also don't grant a session immortality in the store the way a
reported todo does — nearly every plan contains a step nobody will ever do.

## The memory store

Sessions are chunked and embedded into `~/.agent-recap/store.db` (SQLite +
[sqlite-vec](https://github.com/asg017/sqlite-vec), one file, easy to delete).

Raw transcripts are **not** embedded — Claude Code alone is ~200 MB of mostly tool
output. Instead each session yields a handful of chunks: a summary, the last message
from each side, open todos, and any plan document it produced. Plan documents turn
out to be the most valuable material, since they hold the decisions and rationale.

`ask` embeds your question, runs KNN over the chunks, reranks with a mild recency
bias, caps each session at two chunks so one chatty session can't crowd out the
answer, then asks the model to answer with citations. Use `ask --json` to inspect
what was retrieved — retrieval failures and generation failures look identical
otherwise.

### Expiry

Age alone is a bad rule: a project you paused two months ago may be exactly the one
with unfinished work. So expiry is tiered, in this order:

1. Pinned → never expires.
2. Has an open todo → never expires.
3. Project directory no longer exists → dropped immediately, however recent.
4. Active within `max_age_days` (default 90) → kept.
5. Otherwise → dropped.

`prune --dry-run` shows the verdict before anything is deleted.

## Speed

Summarizing is the slow part (~10s per batch of six on an M2 Pro). Each session is
fingerprinted on its content, so unchanged sessions reuse their cached recap and
skip re-embedding entirely:

- cold run, 36 sessions: ~50s
- warm run, nothing changed: no model calls at all
- `--no-llm`, 67 sessions over 90 days: ~3.5s

Each project also contributes at most four sessions, so a project with seven open
sessions doesn't generate seven recaps.

## Recap quality

The summarizer is given the last few turns, the files the agent edited, the open
todos, the branch and the uncommitted-file count — not just the final message.
Harness-generated turns (`<task-notification>`, `<system-reminder>`, slash-command
echoes) are filtered out first, since they aren't things you said and they crowd
out the real content.

`qwen3:8b` is good enough for daily use. `--summarizer claude` is meaningfully
sharper if you want it for a particular run.

## Secrets

Excerpts are scrubbed before being stored, before being sent to the model, and
before being rendered: provider API keys, GitHub and Slack tokens, AWS key ids,
JWTs, private key blocks, `KEY=value` assignments and long base64/hex runs are
replaced with `«redacted»`. Field *names* are preserved so the recap still reads
sensibly.

This matters more for the store than the page, because the store persists after the
session that produced it is gone. `tests/test_scrub.py` asserts that no raw secret
material survives.

## Configuration

Optional `~/.agent-recap/config.toml`:

```toml
chat_model   = "qwen3:8b"
embed_model  = "nomic-embed-text"
days         = 7
limit        = 12
max_age_days = 90
sources      = ["claude-code", "cursor", "vscode"]
```

Changing `embed_model` to one with a different dimension needs a rebuild:
`rm ~/.agent-recap/store.db && agent-recap index`. `doctor` catches the mismatch.

The store also records a `chunker_version`. Chunk shapes changing invalidates
every stored chunk automatically, because the per-session fingerprint tracks
session content and would otherwise happily keep stale chunks forever.

## Tests

```sh
uv run pytest tests/ -q
```

76 tests covering secret scrubbing, plan-step extraction and disk-based
assessment, the reported/inferred todo split, project grouping and ranking, the
tiered expiry rules (including that pruning cascades to the vector table rather
than orphaning rows), the VS Code journal replay, the format-drift probes, the
synthetic-turn filter, the launchd TCC guard, and the read-only guarantee against
the live Cursor and VS Code databases.

## License

[MIT](LICENSE).

## When these formats change

They will — none of this storage is documented or stable. Each source has a probe
that checks its schema landmarks (expected tables, columns and fields) and reports
through `agent-recap doctor`. If a collector returns nothing while its store clearly
holds data, the run says so, so drift looks like a broken reader instead of a quiet
"no sessions found".

## Scheduling notes

`schedule --at HH:MM` writes a launchd agent and loads it. Two things that bite:

- **macOS hides `~/Documents`, `~/Desktop` and `~/Downloads` from launchd agents.**
  A scheduled job whose interpreter or package source lives there dies with `EPERM`
  before running any code. `schedule` refuses to install in that case and tells you
  what to do.
- **Use a plain `uv tool install`, not `--editable`.** An editable install leaves the
  source in the checkout, so launchd still can't read it.
- **After changing the code, reinstall with `--reinstall`:**
  `uv tool install --force --reinstall <path>`. `--force` on its own reuses a cached
  wheel and silently leaves the old code installed.

Logs are at `~/.agent-recap/schedule.log` and `schedule.err.log`.

## Caveats

- macOS only, and it reads undocumented storage that these tools may change.
- Claude Code message counts are approximate: the tail of a large transcript is read
  rather than all of it, deliberately.
- Cursor's plan documents are matched to sessions by name, because Cursor's own
  `referencedPlans` field is empty in practice. Claude Code's `slug` is an exact join.
- Plan documents written as pure design (numbered sections rather than a build
  order) yield no steps at all. That is deliberate: 4 of 12 plans here produce
  nothing, which is better than inventing todos out of prose.
- Cursor's message previews are sometimes truncated to near-nothing by Cursor itself.
