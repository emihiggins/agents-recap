"""Plan documents are prose, not checklists -- extraction must not invent work."""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from agent_recap.plans import MAX_STEPS, extract_steps


def test_build_order_numbered_items():
    steps = extract_steps("""
# Plan

## Context
Some background prose that is not a step.

## Build order

1. `npm create astro@latest` into the new directory.
2. `astro.config.mjs`, `.nvmrc`, `.gitignore`.
3. Wire up the layout.
""")
    assert steps == [
        "npm create astro@latest into the new directory.",
        "astro.config.mjs, .nvmrc, .gitignore.",
        "Wire up the layout.",
    ]


def test_wrapped_continuation_lines_are_joined():
    steps = extract_steps("""
## Build order

1. Install the toolchain and verify the header, nav and dark-mode flip
   on a blank page before any content exists.
2. Second thing.
""")
    assert len(steps) == 2
    assert "on a blank page" in steps[0]


def test_context_sections_are_ignored():
    """Numbered lists outside a work section are argument, not work."""
    steps = extract_steps("""
## Why a rewrite, not a fork

1. The fork has 200 open issues.
2. The API is wrong.

## Prior art
1. Something else entirely.
""")
    assert steps == []


def test_phase_headings_become_items_without_their_detail():
    """A phase heading is the work item; its body is implementation detail."""
    steps = extract_steps("""
## Phase 1 — Toolchain & manifest

- deploymentTarget: iOS "26.0"
- SWIFT_VERSION: "6.0"

## Phase 2 — Ledger integrity

- some other detail
""")
    assert steps == ["Phase 1 — Toolchain & manifest", "Phase 2 — Ledger integrity"]


def test_nested_bullets_are_detail_and_are_dropped():
    """Sub-bullets are implementation detail; keeping them makes cards unreadable.

    The plan document itself remains the place to go for detail.
    """
    steps = extract_steps("""
## Next steps

1. Packaged build
   - app icon
   - notarization
2. Command palette
""")
    assert steps == ["Packaged build", "Command palette"]


def test_code_fences_are_skipped():
    steps = extract_steps("""
## Build order

```sh
1. this is sample output, not a step
```

1. A real step.
""")
    assert steps == ["A real step."]


def test_markdown_is_stripped():
    steps = extract_steps("""
## Next steps

1. **Packaged build** — see [the docs](http://x) and `run --this`
""")
    assert steps == ["Packaged build — see the docs and run --this"]


def test_duplicates_are_collapsed():
    steps = extract_steps("""
## Build order
1. Write the tests.

## Next steps
1. Write the tests.
2. Ship it.
""")
    assert steps == ["Write the tests.", "Ship it."]


def test_step_count_is_capped():
    body = "\n".join(f"{i}. Step number {i}." for i in range(1, 40))
    assert len(extract_steps(f"## Build order\n\n{body}")) == MAX_STEPS


def test_plan_with_no_work_section_yields_nothing():
    """Design-only plans must produce no todos rather than fabricated ones."""
    steps = extract_steps("""
## Context
Prose.

## A1. The central decision
Discussion.

## A2. The containment boundary
More discussion.
""")
    assert steps == []


def test_empty_input():
    assert extract_steps("") == []


def test_file_evidence_reads_disk(tmp_path):
    from agent_recap.plans import file_evidence

    (tmp_path / "pyproject.toml").write_text("x")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "cli.py").write_text("x")

    evidence = file_evidence("pyproject.toml, pkg/cli.py, missing.py", str(tmp_path))
    assert evidence == {"pyproject.toml": True, "pkg/cli.py": True, "missing.py": False}


def test_file_evidence_ignores_non_paths(tmp_path):
    from agent_recap.plans import file_evidence

    assert file_evidence("Do this e.g. carefully, v1.2.3 of the spec", str(tmp_path)) == {}


def test_file_evidence_skips_heavy_directories(tmp_path):
    """node_modules must not be walked, or indexing a JS repo would crawl."""
    from agent_recap.plans import _project_index, _index_cache

    heavy = tmp_path / "node_modules" / "pkg"
    heavy.mkdir(parents=True)
    (heavy / "index.js").write_text("x")
    (tmp_path / "real.js").write_text("x")

    _index_cache.clear()
    _, basenames = _project_index(str(tmp_path))
    assert "real.js" in basenames
    assert "index.js" not in basenames


def test_assess_marks_done_when_all_files_exist(tmp_path):
    from agent_recap.plans import _index_cache, assess

    (tmp_path / "a.py").write_text("x")
    (tmp_path / "b.py").write_text("x")
    _index_cache.clear()
    assert assess(["a.py and b.py"], str(tmp_path)) == [("a.py and b.py", "completed", True)]


def test_assess_marks_pending_when_a_file_is_missing(tmp_path):
    from agent_recap.plans import _index_cache, assess

    (tmp_path / "a.py").write_text("x")
    _index_cache.clear()
    assert assess(["a.py and gone.py"], str(tmp_path)) == [("a.py and gone.py", "pending", True)]


def test_assess_flags_uncheckable_steps_unverified(tmp_path):
    """No file names means we cannot know; show it, but say so."""
    from agent_recap.plans import _index_cache, assess

    _index_cache.clear()
    assert assess(["Phase 2 — Identity & concurrency"], str(tmp_path)) == [
        ("Phase 2 — Identity & concurrency", "pending", False)
    ]


def test_assess_without_a_project_path_is_unverified():
    from agent_recap.plans import assess

    assert assess(["a.py"], None) == [("a.py", "pending", False)]
