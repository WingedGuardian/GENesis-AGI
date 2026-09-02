"""PR flow-rate reporter: the two ways this measurement lies.

Both failure modes were committed by hand before the script existed, which is
why they are the tests:

1. A CAPPED `gh pr list` read looks exactly like a quiet period — weeks fall to
   zero because the page ended, not because nothing happened. The first
   hand-rolled run reported five weeks of zero activity that way.
2. The CURRENT week is partial by definition, so averaging it in drags the
   rate toward zero. The second hand-rolled run reported 79.8/76.7 for what
   was really 78.3/76.3.

A rate that is silently wrong is worse than no rate: it gets written into a
spec as a measured fact.
"""

from __future__ import annotations

import importlib.util
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


def _pr(created_weeks: float, closed_weeks: float | None, state: str = "MERGED") -> dict:
    return {
        "number": 1,
        "createdAt": _iso(created_weeks),
        "mergedAt": _iso(closed_weeks) if closed_weeks is not None else None,
        "closedAt": None,
        "state": state,
    }


@pytest.fixture
def fake_gh(monkeypatch):
    """Replace the gh call; the harness must never hit the network."""

    def _install(prs: list[dict]):
        import json as _json

        def _fake(args):
            if "repo" in args and "view" in args:
                return "owner/repo\n"
            return _json.dumps(prs)

        monkeypatch.setattr(pfr, "_gh", _fake)

    return _install


def test_the_current_partial_week_is_excluded_from_the_rate(fake_gh):
    """Week 0 is still running, so counting it drags the rate down.

    Two complete weeks at 10 opened each, plus a partial week with only 1 so
    far. The rate must read 10.0, not the 7.0 you get by averaging all three.
    """
    prs = [_pr(1.5, 1.5) for _ in range(10)]
    prs += [_pr(2.5, 2.5) for _ in range(10)]
    prs += [_pr(0.2, None, state="OPEN")]
    fake_gh(prs)

    # weeks=3 -> scores weeks 1 and 2 only. Asking for 4 would score an
    # empty week 3 as a real zero and drag the rate to 6.7, which is
    # correct behaviour for a complete sample and simply the wrong fixture.
    report = pfr.collect(weeks=3, limit=800, repo="owner/repo", now=NOW)

    assert report["open_rate_per_week"] == 10.0, report["rows"]
    week0 = next(r for r in report["rows"] if r["weeks_ago"] == 0)
    assert week0["complete"] is False
    assert week0["opened"] == 1, "the partial week is still REPORTED, just not scored"


def test_a_capped_sample_omits_old_weeks_instead_of_calling_them_zero(fake_gh):
    """The failure that produced five fabricated weeks of silence.

    With the sample capped, nothing is known before the oldest PR returned.
    Those weeks must not appear as rows reading 0/0 — that is indistinguishable
    from a genuinely quiet week and averages into the rate as one.
    """
    prs = [_pr(1.5, 1.5) for _ in range(5)]  # nothing older than ~2 weeks
    fake_gh(prs)

    report = pfr.collect(weeks=10, limit=5, repo="owner/repo", now=NOW)

    assert report["sample_truncated"] is True
    weeks_present = {r["weeks_ago"] for r in report["rows"]}
    assert 5 not in weeks_present and 9 not in weeks_present, (
        f"weeks beyond the sample horizon were emitted as data: {sorted(weeks_present)}"
    )
    assert "hit the" in pfr.render(report), "truncation must be stated in the output"


def test_an_untruncated_sample_is_not_flagged(fake_gh):
    """The positive control: without it, a check that always warns is useless."""
    fake_gh([_pr(1.5, 1.5) for _ in range(5)])
    report = pfr.collect(weeks=3, limit=800, repo="owner/repo", now=NOW)
    assert report["sample_truncated"] is False
    # NOT `"cap" not in ...` — "capacity" appears in the GROWING verdict, so
    # that assertion fails on a perfectly untruncated sample, for a reason
    # that has nothing to do with truncation.
    assert "hit the" not in pfr.render(report)


def test_growing_and_draining_verdicts_are_distinguishable(fake_gh):
    """The verdict is the whole point — it must flip on the real condition."""
    fake_gh([_pr(1.5, 1.5) for _ in range(3)] + [_pr(1.5, None, state="OPEN") for _ in range(7)])
    growing = pfr.collect(weeks=3, limit=800, repo="owner/repo", now=NOW)
    assert growing["draining"] is False
    assert "GROWING" in pfr.render(growing)

    # To DRAIN, the closes inside the window must exceed the opens inside it —
    # so the backlog has to have been opened BEFORE the window. Any fixture
    # where each PR both opens and closes inside the scored weeks averages to
    # equal rates and correctly reads GROWING (a flat queue is not draining).
    fake_gh([_pr(6.0, 1.5) for _ in range(9)])
    draining = pfr.collect(weeks=3, limit=800, repo="owner/repo", now=NOW)
    assert draining["draining"] is True
    assert "DRAINING" in pfr.render(draining)


def test_a_failed_gh_call_raises_rather_than_reporting_an_empty_queue(monkeypatch):
    """Fail LOUD. An empty result must never read as 'no PRs, all clear' —
    that is the silent-all-clear shape the house rules ban at data boundaries."""
    import subprocess

    def _boom(*a, **kw):
        return subprocess.CompletedProcess(a, 1, "", "gh: auth required")

    monkeypatch.setattr(subprocess, "run", _boom)
    with pytest.raises(RuntimeError, match="auth required"):
        pfr._gh(["pr", "list"])
