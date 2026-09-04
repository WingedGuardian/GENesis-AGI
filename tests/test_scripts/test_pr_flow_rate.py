"""PR flow-rate reporter: the ways this measurement lies.

A rate that is silently wrong is worse than no rate — it gets written into a
spec as a measured fact, which is exactly what happened here (see CLAUDE.md's
closing-rate bullet, whose figure this script's first version produced).

Two failure modes were committed by hand before the script existed:

1. A CAPPED `gh pr list` read looks exactly like a quiet period — weeks fall to
   zero because the page ended, not because nothing happened. The first
   hand-rolled run reported five weeks of zero activity that way.
2. Averaging a week that is only partly sampled drags the rate toward whatever
   the missing rows would have been.

Eight review findings then landed on the first script (PR #1613), and they were
ONE defect wearing eight faces: it printed a confident GROWING/DRAINING verdict
from a sample whose completeness it never established. So the tests below are
organised around COMPLETENESS — which weeks the sample can speak for, and what
the script is allowed to say when it cannot speak for any.

Filesystem-only: `_gh` is replaced, so no test here touches the network.
"""

from __future__ import annotations

import importlib.util
import json as _json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "pr_flow_rate", Path(__file__).parents[2] / "scripts" / "pr_flow_rate.py"
)
pfr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(pfr)

NOW = datetime(2026, 9, 2, 12, 0, 0, tzinfo=UTC)


def _iso(weeks_ago: float) -> str:
    return (NOW - timedelta(weeks=weeks_ago)).isoformat().replace("+00:00", "Z")


@pytest.fixture
def fake_gh(monkeypatch):
    """Replace the gh call, ROUTING BY POPULATION.

    The script now issues three independent queries, and routing them to one
    shared list is what the old harness did — which is precisely the conflation
    the P1 is about. A fixture that cannot tell openings from closures cannot
    catch a bug about telling openings from closures.

    Each argument is a list of week-offsets (floats, weeks ago).
    """

    def _install(opened: list[float], closed: list[float], open_now: int = 0):
        def _fake(args):
            if "repo" in args and "view" in args:
                return "owner/repo\n"
            search = args[args.index("--search") + 1] if "--search" in args else ""
            state = args[args.index("--state") + 1]
            if state == "open":
                rows = [{"number": i} for i in range(open_now)]
            elif search.startswith("created:"):
                rows = [{"number": i, "createdAt": _iso(w)} for i, w in enumerate(opened)]
            else:
                rows = [
                    {"number": i, "closedAt": _iso(w), "mergedAt": None}
                    for i, w in enumerate(closed)
                ]
            limit = int(args[args.index("--limit") + 1])
            return _json.dumps(rows[:limit])

        monkeypatch.setattr(pfr, "_gh", _fake)

    return _install


class TestTheSampleMustSupportTheVerdict:
    def test_the_trailing_seven_days_are_scored_not_discarded(self, fake_gh):
        """Bucket 0 is a COMPLETE week, and the first version threw it away.

        `_weeks_ago` counts backward from `now`, so bucket 0 is the rolling
        seven-day interval ending at this instant — a full week of elapsed
        time, not a partial calendar week. Excluding it dropped every event
        from the last seven days and made the published rate a week stale,
        which reverses the verdict whenever flow changes quickly (Codex P2,
        PR #1613).

        Fixture: 10/wk for two older weeks, then a BURST of 30 in the last
        seven days. Dropping bucket 0 reads 10.0/wk; scoring it reads 16.7.
        """
        opened = [1.5] * 10 + [2.5] * 10 + [0.2] * 30
        fake_gh(opened, closed=[], open_now=1)
        report = pfr.collect(weeks=3, limit=800, repo="owner/repo", now=NOW)

        week0 = next(r for r in report["rows"] if r["weeks_ago"] == 0)
        assert week0["complete"] is True
        assert report["open_rate_per_week"] == pytest.approx(16.7, abs=0.05), report["rows"]

    def test_a_capped_query_excludes_its_own_oldest_week_too(self, fake_gh):
        """The cap can fall PARTWAY THROUGH the oldest week it returns, so that
        week's counts are a floor rather than a total. Scoring it averages an
        underreported week into the rate (Codex P2, PR #1613).

        `gh pr list` returns NEWEST first, so a cap cuts the OLD end: weeks
        newer than the oldest returned row are trustworthy, that week is not.

        Fixture: 3 openings in week 0 and 2 in week 1, capped at 5. Week 1 is
        the oldest the query can see — the cap may have fallen inside it — so
        week 0 is scored and week 1 is not.
        """
        fake_gh(opened=[0.2] * 3 + [1.5] * 2, closed=[], open_now=0)
        report = pfr.collect(weeks=10, limit=5, repo="owner/repo", now=NOW)

        assert report["samples"]["opened"]["truncated"] is True
        assert report["complete_weeks_scored"] == 1, report["rows"]
        week1 = next(r for r in report["rows"] if r["weeks_ago"] == 1)
        assert week1["complete"] is False, "the cap-boundary week was scored as complete"
        assert week1["opened"] == 2, "the boundary week is still REPORTED, just not scored"
        assert "hit its" in pfr.render(report), "truncation must be stated in the output"

    def test_an_untruncated_sample_is_not_flagged(self, fake_gh):
        """CONTROL: without it, a check that always warns is useless.

        NOT `"cap" not in ...` — "capacity" appears in the GROWING verdict, so
        that assertion fails on a perfectly untruncated sample for a reason
        that has nothing to do with truncation.
        """
        fake_gh(opened=[1.5] * 5, closed=[1.5] * 5, open_now=2)
        report = pfr.collect(weeks=3, limit=800, repo="owner/repo", now=NOW)
        assert not any(s["truncated"] for s in report["samples"].values())
        assert "hit its" not in pfr.render(report)

    def test_a_truncated_openings_query_cannot_corrupt_the_closure_count(self, fake_gh):
        """THE P1. Openings and closures are different populations, and the old
        single `--state all` fetch was ordered by CREATION — so hitting its cap
        dropped PRs created long ago, which are exactly the ones that may have
        CLOSED this week or still be open today. Closures and backlog were
        understated by a cap that had nothing to do with them, and the verdict
        could flip on it.

        Fixture: the openings query fills its cap of 5 (truncated) while the
        closures and backlog queries do not. The three answers disagree — 5, 1
        and 3 — which they cannot do if one fetch feeds all of them. Under the
        single-fetch design the backlog was counted as the `state == "OPEN"`
        rows INSIDE that capped sample, so it was a function of the cap.
        """
        fake_gh(opened=[0.2] * 5, closed=[0.2], open_now=3)
        report = pfr.collect(weeks=3, limit=5, repo="owner/repo", now=NOW)

        assert report["samples"]["opened"]["truncated"] is True
        assert report["samples"]["closed"]["truncated"] is False, (
            "one query's cap was applied to another population"
        )
        assert report["samples"]["open_now"]["truncated"] is False
        week0 = next(r for r in report["rows"] if r["weeks_ago"] == 0)
        assert week0["closed"] == 1, "the closure count followed the openings cap"
        assert report["currently_open"] == 3, "the backlog was derived from the wrong sample"


class TestTheVerdict:
    def test_growing_and_draining_are_distinguishable(self, fake_gh):
        """The verdict is the whole point — it must flip on the real condition."""
        fake_gh(opened=[1.5] * 10, closed=[1.5] * 3, open_now=7)
        growing = pfr.collect(weeks=3, limit=800, repo="owner/repo", now=NOW)
        assert growing["verdict"] == "GROWING"
        assert "GROWING" in pfr.render(growing)

        fake_gh(opened=[1.5] * 3, closed=[1.5] * 9, open_now=4)
        draining = pfr.collect(weeks=3, limit=800, repo="owner/repo", now=NOW)
        assert draining["verdict"] == "DRAINING"
        assert "DRAINING" in pfr.render(draining)

    def test_equal_flow_is_flat_and_not_growing(self, fake_gh):
        """A queue that holds its size is not growing "without bound", and the
        false verdict can trigger closing-capacity changes nobody needed. Exact
        equality is ordinary over a short integer-count window (Codex P2,
        PR #1613)."""
        fake_gh(opened=[1.5] * 8, closed=[1.5] * 8, open_now=5)
        report = pfr.collect(weeks=3, limit=800, repo="owner/repo", now=NOW)
        assert report["verdict"] == "FLAT"
        assert report["draining"] is False
        rendered = pfr.render(report)
        assert "FLAT" in rendered and "without bound" not in rendered

    def test_no_complete_week_says_unknown_rather_than_guessing(self, fake_gh):
        """With nothing scoreable the script must WITHHOLD the verdict. The old
        version fell through to the GROWING branch, so a sample that supported
        no conclusion still printed one."""
        fake_gh(opened=[0.2] * 3, closed=[], open_now=1)
        report = pfr.collect(weeks=10, limit=3, repo="owner/repo", now=NOW)
        assert report["complete_weeks_scored"] == 0
        assert report["verdict"] == "UNKNOWN"
        assert "UNKNOWN" in pfr.render(report)
        assert "GROWING" not in pfr.render(report)

    def test_a_tiny_positive_net_still_renders_a_clearance_estimate(self, fake_gh):
        """`net_per_week` is ROUNDED for display and hits 0.0 for a real net
        below 0.05/wk — reachable with a one-PR difference over 22+ weeks. The
        old code divided the backlog by that rounded value, so `draining` was
        true and the render raised ZeroDivisionError (Codex P2, PR #1613).

        One extra closure across 25 complete weeks: net = 0.04/wk.
        """
        fake_gh(
            opened=[w + 0.5 for w in range(25)],
            closed=[0.2] + [w + 0.5 for w in range(25)],
            open_now=3,
        )
        report = pfr.collect(weeks=25, limit=800, repo="owner/repo", now=NOW)

        assert report["verdict"] == "DRAINING"
        assert report["net_per_week"] == 0.0, "the fixture no longer exercises the rounding"
        assert report["net_per_week_raw"] > 0
        assert "DRAINING" in pfr.render(report)  # must not raise


class TestSamplingArgumentsAreValidated:
    """`--limit 0` returned an empty fetch that read as "no PRs", scored every
    week as complete-and-zero, and printed the factual verdict GROWING;
    `--weeks -1` queried the API and reported no complete weeks. Neither value
    can represent a sample, so neither reaches a query (Codex P2, PR #1613)."""

    @pytest.mark.parametrize("bad", ["0", "-1", "-800"])
    def test_non_positive_is_rejected(self, bad):
        with pytest.raises(Exception, match="positive integer"):
            pfr._positive(bad)

    def test_positive_values_pass_through(self):
        assert pfr._positive("1") == 1
        assert pfr._positive("800") == 800


def test_reopens_are_declared_not_silently_ignored(fake_gh):
    """The metric counts creations and closures; a reopen is neither, so `net`
    is a flow rate and NOT the exact backlog change. Counting reopens needs a
    per-PR timeline call — a different cost class — so the requirement is that
    the script SAYS so rather than letting the number be read as something it
    is not (Codex P2, PR #1613)."""
    fake_gh(opened=[1.5] * 4, closed=[1.5] * 4, open_now=2)
    report = pfr.collect(weeks=3, limit=800, repo="owner/repo", now=NOW)
    assert report["counts_reopens"] is False
    assert "reopen" in pfr.render(report).lower()


def test_a_failed_gh_call_raises_rather_than_reporting_an_empty_queue(monkeypatch):
    """Fail LOUD. An empty result must never read as 'no PRs, all clear' —
    that is the silent-all-clear shape the house rules ban at data boundaries."""
    import subprocess

    def _boom(*a, **kw):
        return subprocess.CompletedProcess(a, 1, "", "gh: auth required")

    monkeypatch.setattr(subprocess, "run", _boom)
    with pytest.raises(RuntimeError, match="auth required"):
        pfr._gh(["pr", "list"])
