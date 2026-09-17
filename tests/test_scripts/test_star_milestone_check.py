"""The star-milestone watcher: announce once on a crossing, stay silent otherwise.

WHAT THESE PIN, and why each one is here rather than assumed:

The watcher's whole value is that it fires EXACTLY ONCE per milestone and never
silently misses one. Both halves of that are failure modes with opposite fixes —
firing repeatedly is noise that gets muted, and missing a crossing turns the
parked work back into something waiting to be remembered, which is the state the
watcher exists to end. So the suite drives the transition, the repeat, and every
path that could swallow a crossing.

The other axis is the FAIL DIRECTION, which is not symmetric. A count that could
not be read must exit non-zero (a run that verified nothing must not look like a
run that found no news), while an install with no repo configured must exit ZERO
(bootstrap enables this timer on every clone, and a fresh one has nothing to
watch — a daily red unit there would be a defect this file ships, not a signal).

Install-agnostic: the network and the database are both stubbed, no real HTTP,
no real writes.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parent.parent.parent
_SCRIPT = _REPO / "scripts" / "star_milestone_check.py"


def _load():
    spec = importlib.util.spec_from_file_location("_star_milestone_check", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_star_milestone_check"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load()


@pytest.fixture
def wired(monkeypatch, tmp_path):
    """Point the module at a scratch state file and stub its two side effects.

    Returns a dict the test reads back: every announcement the module attempted.
    """
    monkeypatch.setattr(mod, "_STATE_PATH", tmp_path / "star_milestones.json")
    monkeypatch.setattr(mod, "_slug", lambda: "owner/repo")
    announced: list[tuple[int, int]] = []

    def _fake_announce(slug, milestone, count):
        announced.append((milestone, count))
        return True

    # `main` calls asyncio.run(_announce(...)); replace the whole call so the
    # test never needs a database or an event loop.
    monkeypatch.setattr(mod, "asyncio", type("A", (), {"run": staticmethod(lambda coro: coro)})())
    monkeypatch.setattr(mod, "_announce", _fake_announce)
    return {"announced": announced, "state": tmp_path / "star_milestones.json"}


def _at(monkeypatch, count: int) -> None:
    monkeypatch.setattr(mod, "_star_count", lambda slug: count)


# ---------------------------------------------------------------------------
# THE TRANSITION, AND THE REPEAT.
# ---------------------------------------------------------------------------


def test_crossing_announces_once_and_a_second_run_is_silent(wired, monkeypatch):
    """THE POINT. Firing every day would be noise that gets muted, and a muted
    watcher is the same as no watcher."""
    _at(monkeypatch, 201)
    assert mod.main() == 0
    assert wired["announced"] == [(200, 201)]

    # State persisted, so the next run has something to compare against.
    assert json.loads(wired["state"].read_text())["highest_announced"] == 200

    assert mod.main() == 0
    assert wired["announced"] == [(200, 201)], "announced the same milestone twice"


def test_below_the_milestone_says_nothing(wired, monkeypatch):
    _at(monkeypatch, 199)
    assert mod.main() == 0
    assert wired["announced"] == []
    assert not wired["state"].exists(), "wrote state without announcing anything"


def test_exactly_on_the_boundary_counts_as_crossed(wired, monkeypatch):
    """`>=`, not `>`. 200 stars IS the 200 milestone, and an off-by-one here
    delays the wake-up until the next star arrives — which may be never."""
    _at(monkeypatch, 200)
    assert mod.main() == 0
    assert wired["announced"] == [(200, 200)]


def test_a_jump_past_several_milestones_announces_only_the_highest(wired, monkeypatch):
    """A repo that gains a thousand stars between two runs should produce ONE
    observation, not four — and the highest is the one whose parked work is most
    likely to matter."""
    _at(monkeypatch, 2600)
    assert mod.main() == 0
    assert wired["announced"] == [(2500, 2600)]
    assert json.loads(wired["state"].read_text())["highest_announced"] == 2500


def test_the_next_milestone_after_one_already_announced_still_fires(wired, monkeypatch):
    """Announcing 200 must not disarm 500. The state records the HIGHEST
    announced, not 'done'."""
    _at(monkeypatch, 201)
    assert mod.main() == 0
    _at(monkeypatch, 501)
    assert mod.main() == 0
    assert wired["announced"] == [(200, 201), (500, 501)]


# ---------------------------------------------------------------------------
# FAIL DIRECTIONS. Not symmetric, and that asymmetry is the design.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "boom",
    [
        urllib.error.URLError("no route"),
        TimeoutError("timed out"),
        ValueError("stargazers_count missing or not an int: None"),
    ],
    ids=["network", "timeout", "malformed-payload"],
)
def test_an_unreadable_count_exits_nonzero_and_touches_nothing(wired, monkeypatch, boom):
    """A run that could not READ the count must not resemble a run that found no
    news. It exits non-zero, writes no state, and announces nothing — tomorrow
    retries from the same place."""

    def _raise(slug):
        raise boom

    monkeypatch.setattr(mod, "_star_count", _raise)
    assert mod.main() == 1
    assert wired["announced"] == []
    assert not wired["state"].exists()


def test_an_unconfigured_install_is_a_clean_no_op(monkeypatch):
    """Bootstrap enables every shipped timer on EVERY clone. A fresh install has
    no github.user, and a daily failing unit there would be a defect we shipped,
    not a signal. Exit 0, announce nothing."""
    monkeypatch.setattr(mod, "_slug", lambda: None)
    called: list[str] = []
    monkeypatch.setattr(mod, "_star_count", lambda slug: called.append(slug) or 9999)
    assert mod.main() == 0
    assert called == [], "read the count for an install with no repo configured"


def test_a_corrupt_state_file_announces_rather_than_swallowing(wired, monkeypatch):
    """The tie breaks toward announcing. A duplicate observation is an
    annoyance; a silent miss is the failure this script exists to prevent — and
    the real duplicate guard is the stable content hash in `_announce`, not this
    file."""
    wired["state"].write_text("{not json at all")
    _at(monkeypatch, 201)
    assert mod.main() == 0
    assert wired["announced"] == [(200, 201)]


def test_a_failed_announcement_exits_nonzero_and_does_not_record_it(wired, monkeypatch):
    """If the observation could not be written, the milestone was NOT announced.
    Recording it anyway would mean the crossing is never announced at all —
    the one outcome worse than announcing twice."""

    def _boom(slug, milestone, count):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(mod, "_announce", _boom)
    _at(monkeypatch, 201)
    assert mod.main() == 1
    assert not wired["state"].exists(), "recorded a milestone that was never announced"


# ---------------------------------------------------------------------------
# CONFIGURATION.
# ---------------------------------------------------------------------------


def test_milestones_can_be_overridden_and_are_sorted_and_deduped(monkeypatch):
    monkeypatch.setenv("GENESIS_STAR_MILESTONES", "500, 100,100 , 250")
    assert mod._milestones() == [100, 250, 500]


def test_a_malformed_override_refuses_rather_than_falling_back(monkeypatch):
    """Silently reverting to defaults would watch a number the operator did not
    ask for, and they would never learn the override was ignored."""
    monkeypatch.setenv("GENESIS_STAR_MILESTONES", "200,not-a-number")
    with pytest.raises(SystemExit):
        mod._milestones()


def test_an_empty_override_refuses_too(monkeypatch):
    monkeypatch.setenv("GENESIS_STAR_MILESTONES", " , ,")
    with pytest.raises(SystemExit):
        mod._milestones()


def test_the_observation_type_is_permanent(monkeypatch):
    """The observation IS the wake-up. Under the 14-day default TTL it would
    expire before anyone acted on it, and the watcher would be a no-op nobody
    noticed. Pinned here because the type and the TTL policy live in different
    files, so nothing else would catch them drifting apart."""
    sys.path.insert(0, str(_REPO / "src"))
    from genesis.db.crud import observations

    assert "repo_milestone_reached" in observations._PERMANENT_TYPES
    assert "repo_milestone_reached" not in observations.INTERNAL_OBS_TYPES, (
        "a milestone the user never sees cannot wake anything up"
    )
