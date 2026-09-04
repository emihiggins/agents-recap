"""Render a sample recap from fictional data, for the README screenshot.

The real recap at ~/.agent-recap/recap.html is full of private project names,
paths and conversation tails, so it cannot be published. This builds the same
page from invented sessions by calling the real renderer, which means the
sample cannot drift away from what the tool actually produces.

    python scripts/sample_recap.py            # -> docs/sample-recap.html
    python scripts/sample_recap.py out.html
"""

from __future__ import annotations

import pathlib
import sys
from datetime import timedelta

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from agent_recap.grouping import group, rank
from agent_recap.models import GitState, Session, Todo, now
from agent_recap.render import render

NOW = now()


def ago(minutes: int):
    return NOW - timedelta(minutes=minutes)


def todo(text: str, status: str = "pending", *, origin: str = "tool",
         verified: bool = True) -> Todo:
    return Todo(text=text, status=status, origin=origin, verified=verified)


SESSIONS = [
    # Blocked on a permission prompt -- drives the "waiting on you" banner.
    Session(
        source="claude-code",
        session_id="s-storefront-1",
        project_path="~/code/storefront-web",
        title="checkout: coupon stacking returns the wrong total",
        last_active_at=ago(4),
        started_at=ago(96),
        live=True,
        live_state="waiting",
        waiting_for="permission to run the migration",
        state_since=ago(11),
        branch="fix/coupon-stacking-total",
        model="claude-opus-5",
        message_count=142,
        recap=(
            "Traced the wrong order total to discounts being applied per line item "
            "instead of once per cart, so stacked coupons compounded. Rewrote the "
            "reducer to fold coupons into a single cart-level adjustment and added "
            "a regression test for two overlapping percentage coupons."
        ),
        next_step="Approve the migration so the backfill can run against staging.",
        todos=[
            todo("fold coupon application into a single cart-level pass", "completed"),
            todo("regression test for two stacked percentage coupons", "completed"),
            todo("backfill historical orders with corrected totals", "in_progress"),
            todo("check the tax line still rounds after the change", "pending"),
        ],
        last_user_text="the totals still look off for two 10% coupons, can you check the rounding",
        last_assistant_text=(
            "The rounding was happening twice -- once per coupon, then again on the "
            "cart total. I moved it to a single rounding step at the end. Running the "
            "backfill needs the migration applied first; may I run it?"
        ),
        git=GitState(
            branch="fix/coupon-stacking-total",
            dirty_files=6,
            ahead=2,
            last_commit="single cart-level coupon pass",
        ),
    ),
    # A second, quieter session on the same project -- exercises grouping.
    Session(
        source="cursor",
        session_id="s-storefront-2",
        project_path="~/code/storefront-web",
        title="pull the price formatter into a shared helper",
        last_active_at=ago(58),
        started_at=ago(140),
        message_count=31,
        recap=(
            "Extracted the currency formatter used by cart, checkout and receipts "
            "into one helper so the coupon fix only had to change rounding in a "
            "single place."
        ),
        todos=[todo("swap the receipt view over to the shared formatter", "pending")],
    ),
    # Live and working.
    Session(
        source="claude-code",
        session_id="s-api-1",
        project_path="~/code/rates-api",
        title="cache the FX rate lookup",
        last_active_at=ago(1),
        started_at=ago(37),
        live=True,
        live_state="busy",
        state_since=ago(3),
        branch="perf/fx-rate-cache",
        model="claude-sonnet-5",
        message_count=64,
        recap=(
            "The quote endpoint was fetching every currency pair on each request, "
            "about 40 upstream calls per quote. Added a 60-second in-process cache "
            "keyed by pair and wired a metric so stale-hit rate is visible."
        ),
        next_step="Load-test the quote endpoint to confirm the p99 actually drops.",
        todos=[
            todo("in-process TTL cache for rate lookups", "completed"),
            todo("emit cache hit/miss metrics", "completed"),
            todo("load-test the quote endpoint at 200 rps", "in_progress"),
        ],
        git=GitState(branch="perf/fx-rate-cache", dirty_files=3, last_commit="add rate cache metrics"),
    ),
    # Unfinished work, ended mid-task, with inferred plan steps.
    Session(
        source="claude-code",
        session_id="s-ios-1",
        project_path="~/code/trailmark-ios",
        title="offline sync for saved routes",
        last_active_at=ago(1500),
        started_at=ago(1720),
        ended_mid_task=True,
        branch="feature/offline-route-sync",
        model="claude-opus-5",
        message_count=207,
        plan_name="plan-offline-sync.md",
        recap=(
            "Built the local SwiftData store and the queue that replays edits made "
            "while offline. Conflict resolution is still last-write-wins, which "
            "loses edits when the same route is changed on two devices."
        ),
        next_step="Decide the conflict rule for a route edited on two devices, then implement it.",
        todos=[
            todo("SwiftData model for cached routes", "completed"),
            todo("replay queue for offline edits", "completed"),
            todo("conflict resolution beyond last-write-wins", "in_progress"),
            todo("surface a sync-failed state in the UI", "pending"),
            todo("phase 3: background refresh on app foreground", "pending",
                 origin="plan", verified=False),
            todo("phase 4: migrate existing users' saved routes", "pending",
                 origin="plan", verified=False),
        ],
        last_user_text="park this for now, but write down where the conflict logic stands",
        last_assistant_text=(
            "Noted. The replay queue is done and tested. The open question is what "
            "wins when a route is edited on two devices -- newest timestamp, or a "
            "merge of the changed fields. I have not implemented either."
        ),
        git=GitState(
            branch="feature/offline-route-sync",
            dirty_files=11,
            ahead=4,
            last_commit="replay queue for offline edits",
        ),
    ),
    # Clean and finished -- shows a row with nothing outstanding.
    Session(
        source="vscode",
        session_id="s-infra-1",
        project_path="~/code/deploy-scripts",
        title="pin the terraform provider versions",
        last_active_at=ago(2900),
        started_at=ago(2960),
        message_count=18,
        recap=(
            "Pinned every provider to an exact version and regenerated the lock file "
            "so plan output stops drifting between machines."
        ),
        todos=[
            todo("pin provider versions", "completed"),
            todo("regenerate the lock file", "completed"),
        ],
        git=GitState(branch="main", last_commit="pin terraform providers"),
    ),
    # No project path -- exercises the unknown-project fallback.
    Session(
        source="cursor",
        session_id="s-scratch-1",
        title="regex for parsing the log timestamps",
        last_active_at=ago(3400),
        message_count=7,
        recap="One-off help writing a regex to pull ISO timestamps out of a log dump.",
    ),
]


def main() -> int:
    out = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else (
        pathlib.Path(__file__).resolve().parents[1] / "docs" / "sample-recap.html"
    )
    groups = rank(group(SESSIONS), limit=12)
    html = render(
        groups,
        store_stats={"sessions": 96, "chunks": 1428},
        total_projects=9,
        total_sessions=41,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    print(f"wrote {out} ({len(html):,} bytes, {len(groups)} projects)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
