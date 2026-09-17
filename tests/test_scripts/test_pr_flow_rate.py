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
import re
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
    """Replace the gh call, ROUTING BY POPULATION and by WEEK WINDOW.

    The script counts each week with its own `search/issues` query and reads
    `total_count`, so the fake has to answer a COUNT for a specific window
    rather than hand back a list of rows. Routing openings and closures to one
    shared answer is what the old harness did, and a fixture that cannot tell
    them apart cannot catch a bug about telling them apart.

    Each argument is a list of week-offsets (floats, weeks ago); the fake
    buckets them the same way the real query's date range does.
    """

    def _install(opened: list[float], closed: list[float], open_now: int = 0):
        def _bucket_counts(offsets: list[float]) -> dict[int, int]:
            counts: dict[int, int] = {}
            for off in offsets:
                counts[int(off)] = counts.get(int(off), 0) + 1
            return counts

        opened_by_week = _bucket_counts(opened)
        closed_by_week = _bucket_counts(closed)

        def _fake(args):
            if "repo" in args and "view" in args:
                return "owner/repo\n"
            q = args[args.index("-f") + 1] if "-f" in args else ""
            if "is:open" in q:
                return f"{open_now}\n"
            # q carries `created:START..END` or `closed:START..END`. Recover
            # the bucket index from START rather than re-deriving it, so the
            # fixture cannot drift from the window the script actually asks
            # for — if the script's arithmetic changes, this stops matching
            # and the test fails loudly instead of quietly agreeing.
            match = re.search(r"(created|closed):(\S+)\.\.", q)
            assert match, f"unrecognised count query: {q!r}"
            field, start = match.group(1), match.group(2)
            start_dt = datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
            week = round((NOW - start_dt).total_seconds() / (7 * 24 * 3600)) - 1
            table = opened_by_week if field == "created" else closed_by_week
            return f"{table.get(week, 0)}\n"

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
        report = pfr.collect(weeks=3, repo="owner/repo", now=NOW)

        week0 = next(r for r in report["rows"] if r["weeks_ago"] == 0)
        assert week0["complete"] is True
        assert report["open_rate_per_week"] == pytest.approx(16.7, abs=0.05), report["rows"]


class TestTheVerdict:
    def test_growing_and_draining_are_distinguishable(self, fake_gh):
        """The verdict is the whole point — it must flip on the real condition."""
        fake_gh(opened=[1.5] * 10, closed=[1.5] * 3, open_now=7)
        growing = pfr.collect(weeks=3, repo="owner/repo", now=NOW)
        assert growing["verdict"] == "GROWING"
        assert "GROWING" in pfr.render(growing)

        fake_gh(opened=[1.5] * 3, closed=[1.5] * 9, open_now=4)
        draining = pfr.collect(weeks=3, repo="owner/repo", now=NOW)
        assert draining["verdict"] == "DRAINING"
        assert "DRAINING" in pfr.render(draining)

    def test_equal_flow_is_flat_and_not_growing(self, fake_gh):
        """A queue that holds its size is not growing "without bound", and the
        false verdict can trigger closing-capacity changes nobody needed. Exact
        equality is ordinary over a short integer-count window (Codex P2,
        PR #1613)."""
        fake_gh(opened=[1.5] * 8, closed=[1.5] * 8, open_now=5)
        report = pfr.collect(weeks=3, repo="owner/repo", now=NOW)
        assert report["verdict"] == "FLAT"
        assert report["draining"] is False
        rendered = pfr.render(report)
        assert "FLAT" in rendered and "without bound" not in rendered

    def test_no_scoreable_week_withholds_the_verdict(self, fake_gh):
        """With nothing scoreable the script must WITHHOLD a verdict.

        RESTORED coverage, not a new test. The rewrite that replaced the capped
        listing with per-week `total_count` queries deleted the old
        `test_no_complete_week_says_unknown_rather_than_guessing`, because it
        passed the now-removed `limit=` argument. But the branch it guarded is
        still live at `scripts/pr_flow_rate.py:218-219` and `:268-269`, so
        deleting the test deleted the coverage, not the behaviour — and the
        defect it originally caught was the script falling through to GROWING
        and printing a conclusion its sample could not support.

        `weeks=0` is the reachable way in now: no week is scored, so `scored`
        is empty and `n == 0`.
        """
        fake_gh(opened=[], closed=[], open_now=1)
        report = pfr.collect(weeks=0, repo="owner/repo", now=NOW)
        assert report["complete_weeks_scored"] == 0
        assert report["verdict"] == "UNKNOWN", (
            "a sample with no scoreable week produced a directional verdict"
        )
        rendered = pfr.render(report)
        assert "UNKNOWN" in rendered
        assert "GROWING" not in rendered
        assert "DRAINING" not in rendered


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
        report = pfr.collect(weeks=25, repo="owner/repo", now=NOW)

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
    report = pfr.collect(weeks=3, repo="owner/repo", now=NOW)
    assert report["counts_reopens"] is False
    assert "reopen" in pfr.render(report).lower()


def test_each_bucket_asks_for_a_true_total_not_a_page(monkeypatch):
    """The central claim of this script, pinned where the fixture cannot.

    `fake_gh` replaces `_gh` wholesale and hands back a number, so it proves
    the arithmetic but never exercises the QUERY — MEASURED: swapping
    `.total_count` for another field left all 15 tests green. Since "ask for an
    exact total instead of counting a capped page" is the entire fix for the
    two P1s on this PR, the one thing no other test could see was worth its own
    assertion.

    An argv contract test, and nothing more: it pins what is asked for, not
    what GitHub returns.
    """
    seen: list[list[str]] = []

    def _record(args):
        seen.append(list(args))
        return "0\n"

    monkeypatch.setattr(pfr, "_gh", _record)
    pfr.collect(weeks=2, repo="owner/repo", now=NOW)

    assert seen, "no query was issued at all"
    for args in seen:
        assert args[:4] == ["api", "-X", "GET", "search/issues"], (
            f"a bucket used something other than the search endpoint: {args}"
        )
        assert "--jq" in args, f"no jq selector, so the field is unpinned: {args}"
        assert args[args.index("--jq") + 1] == ".total_count", (
            "a bucket read a field other than .total_count — a page-derived "
            "number reintroduces exactly the cap-and-ordering defect this "
            f"script was rewritten to remove: {args}"
        )


def test_a_failed_gh_call_raises_rather_than_reporting_an_empty_queue(monkeypatch):
    """Fail LOUD. An empty result must never read as 'no PRs, all clear' —
    that is the silent-all-clear shape the house rules ban at data boundaries."""
    import subprocess

    def _boom(*a, **kw):
        return subprocess.CompletedProcess(a, 1, "", "gh: auth required")

    monkeypatch.setattr(subprocess, "run", _boom)
    with pytest.raises(RuntimeError, match="auth required"):
        pfr._gh(["pr", "list"])
class TestTheWindowsTileTheSpan:
    """The per-week ranges must PARTITION the window — no gap, no overlap.

    This is the defect class the whole redesign exists to remove, turned on
    the redesign itself. GitHub ranges are inclusive at both ends, so a
    bucket whose end is not pulled back one second claims the same instant as
    the next bucket's start, and a PR landing there is counted TWICE — a
    silent overcount of exactly the kind the capped-listing version produced
    by undercounting.

    Found by mutation: removing the one-second pullback left all ten other
    tests green, because the fixture recovers a bucket from its START and
    never looks at the END.
    """

    @staticmethod
    def _parse(stamp: str) -> datetime:
        return datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)

    def test_adjacent_windows_do_not_share_an_instant(self):
        for w in range(6):
            newer_start, _ = pfr._week_window(w, NOW)
            _, older_end = pfr._week_window(w + 1, NOW)
            assert self._parse(older_end) < self._parse(newer_start), (
                f"bucket {w + 1} ends at {older_end}, at or after bucket {w} "
                f"starts at {newer_start} — an event there is counted twice"
            )

    def test_adjacent_windows_leave_no_gap(self):
        # The converse failure: pulling the end back too far drops any event
        # in the hole. One second apart is exactly adjacent at the resolution
        # GitHub search accepts.
        for w in range(6):
            newer_start, _ = pfr._week_window(w, NOW)
            _, older_end = pfr._week_window(w + 1, NOW)
            gap = (self._parse(newer_start) - self._parse(older_end)).total_seconds()
            assert gap == 1, f"bucket {w + 1}->{w} gap is {gap}s, expected 1s"

    def test_each_window_spans_one_week(self):
        for w in range(6):
            start, end = pfr._week_window(w, NOW)
            span = (self._parse(end) - self._parse(start)).total_seconds()
            assert span == 7 * 24 * 3600 - 1, f"bucket {w} spans {span}s"


def test_the_backlog_is_counted_not_derived_from_openings(fake_gh):
    """`currently_open` is its OWN population and must never be inferred.

    A PR opened before the window can still be open today, so deriving the
    backlog from the openings sample is simply wrong — it was wrong in the
    first version and the fix is load-bearing for the DRAINING clearance
    estimate, which divides the backlog by the net rate.

    Fixture makes the two numbers disagree on purpose: 12 openings inside the
    window, but 4 PRs open right now. Any derivation from openings yields 12.

    Found by mutation: replacing the backlog count with `sum(opened.values())`
    left every other test green.
    """
    fake_gh(opened=[1.5] * 12, closed=[1.5] * 12, open_now=4)
    report = pfr.collect(weeks=3, repo="owner/repo", now=NOW)
    assert report["currently_open"] == 4, (
        "the backlog was derived from the openings sample rather than counted"
    )


def test_an_unparseable_total_raises_rather_than_counting_as_zero(monkeypatch):
    """Zero is a LEGITIMATE answer here, which is what makes a silent fallback
    dangerous: an API error, a rate-limit body, or an HTML error page would be
    indistinguishable from a genuinely quiet week and would publish a rate
    derived from nothing.

    `_count` raises for exactly this reason and said so in its docstring, but
    nothing tested it — MEASURED: replacing the raise with `return 0` left the
    whole file green. Distinct from the failed-call test above, which covers
    `gh` exiting non-zero; this covers `gh` succeeding and returning something
    that is not a number.
    """
    monkeypatch.setattr(pfr, "_gh", lambda args: "API rate limit exceeded\n")
    with pytest.raises(RuntimeError, match="total_count"):
        pfr.collect(weeks=2, repo="owner/repo", now=NOW)
