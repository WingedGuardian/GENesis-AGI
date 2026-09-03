"""A process reaped for memory must say so, and must never guess when it cannot know.

Hermetic: every test builds a fake cgroup tree under tmp_path rather than reading the
live one. A test that asserted against this machine's real counter would pass or fail
on whatever else happened to be running, and could not exercise the case that matters
(a reap DURING the watched window) at all.
"""

from __future__ import annotations

import signal
from pathlib import Path

import pytest

from genesis.observability import oom


def _tree(root: Path, rel: str, kills: int | None) -> Path:
    """Create a cgroup dir at ``rel``; ``kills=None`` means no memory.events at all."""
    node = root / rel.lstrip("/")
    node.mkdir(parents=True, exist_ok=True)
    if kills is not None:
        (node / "memory.events").write_text(
            f"low 0\nhigh 0\nmax 0\noom 0\noom_kill {kills}\noom_group_kill 0\n"
        )
    return node


@pytest.fixture
def fake_cgroup(tmp_path, monkeypatch):
    """A realistic v2 layout: a user slice holding BOTH a login scope and a
    systemd-user-manager subtree, which is the shape that matters here."""
    root = tmp_path / "cgroup"
    _tree(root, "/", 0)
    _tree(root, "user.slice", 0)
    _tree(root, "user.slice/user-1000.slice", 7)
    _tree(root, "user.slice/user-1000.slice/session-abc.scope", 0)
    _tree(root, "user.slice/user-1000.slice/user@1000.service", 7)
    _tree(root, "user.slice/user-1000.slice/user@1000.service/genesis-server.service", 0)
    monkeypatch.setattr(oom, "_CGROUP_ROOT", root)

    def _set_own(rel: str) -> None:
        proc = tmp_path / "self_cgroup"
        proc.write_text(f"0::{rel}\n")
        monkeypatch.setattr(oom, "_PROC_SELF_CGROUP", proc)

    _set_own("/user.slice/user-1000.slice/session-abc.scope")
    return root, _set_own


def test_a_sibling_scopes_reap_is_still_counted(fake_cgroup):
    """THE regression. Genesis spawns CC through `systemd-run --scope --user`, which
    puts the child in its own transient scope — a SIBLING of the caller's cgroup, not
    a descendant. Watching the caller's own cgroup therefore reads 0 forever while
    dispatched work is reaped invisibly, which is the exact failure this module
    exists to end.

    Caught by a live smoke test, not by reasoning: the first implementation resolved
    to the nearest readable cgroup, which on this box is the login scope reading 0
    while all 35 real reaps sat one level up.
    """
    root, set_own = fake_cgroup
    set_own("/user.slice/user-1000.slice/session-abc.scope")
    reading = oom.read_oom_kills()
    assert reading is not None
    count, where = reading
    assert count == 7, "resolved to a cgroup that cannot see a sibling scope's reap"
    assert where.endswith("user-1000.slice")


def test_a_systemd_user_unit_resolves_to_the_same_slice(fake_cgroup):
    """The invoker runs inside genesis-server, a systemd USER unit — a different
    starting point that must land on the same watch, or the caller and the child it
    spawns would be watching different counters."""
    _root, set_own = fake_cgroup
    set_own("/user.slice/user-1000.slice/user@1000.service/genesis-server.service")
    reading = oom.read_oom_kills()
    assert reading is not None and reading[0] == 7
    assert reading[1].endswith("user-1000.slice")


def test_an_explicit_cgroup_is_honoured_not_widened(fake_cgroup):
    """A caller that names a cgroup means that one — widening it would silently
    answer a broader question than was asked."""
    reading = oom.read_oom_kills("/user.slice/user-1000.slice/session-abc.scope")
    assert reading is not None and reading[0] == 0
    assert reading[1].endswith("session-abc.scope")


def test_an_unreadable_counter_is_unknown_not_zero(tmp_path, monkeypatch):
    """None and 0 are different facts. Collapsing them would report "no OOM here" on
    a host where the question was never asked — a confident wrong all-clear, which is
    the failure mode this whole module is built to avoid."""
    monkeypatch.setattr(oom, "_CGROUP_ROOT", tmp_path / "cgroup")
    missing = tmp_path / "no_such_proc_file"
    monkeypatch.setattr(oom, "_PROC_SELF_CGROUP", missing)
    assert oom.read_oom_kills() is None
    probe = oom.OomProbe.sample()
    assert probe.available is False
    assert probe.kills_since() is None
    assert probe.describe(-signal.SIGKILL) is None, "must not attribute without evidence"


def test_the_delta_is_what_attributes_not_the_absolute_count(fake_cgroup):
    """A cgroup with a long history of reaps must not make every later death look
    like an OOM. Only a reap DURING the watched window counts."""
    root, _set_own = fake_cgroup
    probe = oom.OomProbe.sample()
    assert probe.baseline == 7
    assert probe.kills_since() == 0
    assert probe.describe(-signal.SIGKILL) is None, "a pre-existing count is not evidence"

    _tree(root, "user.slice/user-1000.slice", 9)  # two reaps during the window
    assert probe.kills_since() == 2
    note = probe.describe(-signal.SIGKILL)
    assert note and "2 process(es)" in note


def test_a_counter_that_moves_backwards_invalidates_rather_than_going_negative(fake_cgroup):
    """A cgroup recreated under us (a restarted unit) resets the counter. That
    invalidates the comparison; it does not prove negative kills."""
    root, _ = fake_cgroup
    probe = oom.OomProbe.sample()
    _tree(root, "user.slice/user-1000.slice", 1)
    assert probe.kills_since() == 0
    assert probe.describe(-signal.SIGKILL) is None


@pytest.mark.parametrize(
    "returncode,self_killed,expected",
    [
        (-signal.SIGKILL, False, True),  # the only attributing case
        (-signal.SIGKILL, True, False),  # our own timeout/reaper — cause already known
        (-signal.SIGTERM, False, False),  # not the OOM killer, whatever the counter says
        (1, False, False),  # ordinary failure
        (0, False, False),
        (None, False, False),  # still running
    ],
)
def test_attribution_requires_sigkill_and_not_our_own_kill(
    fake_cgroup, returncode, self_killed, expected
):
    """Silence is the default in every ambiguous case, because this annotation exists
    to be TRUSTED. A cause line that is sometimes invented is worse than a bare kill,
    which at least does not mislead."""
    root, _ = fake_cgroup
    probe = oom.OomProbe.sample()
    _tree(root, "user.slice/user-1000.slice", 8)  # a real reap in the window
    note = probe.describe(returncode, self_killed=self_killed)
    assert (note is not None) is expected


def test_the_note_names_the_cgroup_it_watched(fake_cgroup):
    """A delta from a broad cgroup is weaker evidence than one from a narrow cgroup,
    so the reader must be able to see which was watched rather than take the
    conclusion on faith."""
    root, _ = fake_cgroup
    probe = oom.OomProbe.sample()
    _tree(root, "user.slice/user-1000.slice", 8)
    note = probe.describe(-signal.SIGKILL)
    assert note is not None
    assert "user-1000.slice" in note
    assert "probable cause" in note, "must not claim certainty it cannot have"


def test_the_victim_premise_is_printed_only_when_it_was_established(fake_cgroup):
    """The note cites oom_score_adj=+500 as the reason THIS process was the likely
    victim. That write can fail — denied procfs, a read-only mount, a process that
    exited first — and `set_oom_score_adj` swallows the OSError. Asserting the
    preference anyway would present an unverified premise as evidence, overstating
    what a counter delta and a SIGKILL actually prove between them.
    """
    root, _ = fake_cgroup
    probe = oom.OomProbe.sample()
    _tree(root, "user.slice/user-1000.slice", 8)

    established = probe.describe(-signal.SIGKILL, score_adjusted=True)
    assert established is not None
    assert "oom_score_adj=+500" in established
    assert "likely victim" in established

    unestablished = probe.describe(-signal.SIGKILL, score_adjusted=False)
    assert unestablished is not None, "the reap itself is still worth reporting"
    assert "oom_score_adj" not in unestablished
    assert "likely victim" not in unestablished
    # What the counter and the signal DO prove stays, either way.
    for note in (established, unestablished):
        assert "probable cause" in note
        assert "1 process(es)" in note
        assert "Reduce the working set" in note


def test_the_victim_premise_defaults_to_unclaimed(fake_cgroup):
    """A caller that does not know whether the write landed must not have the claim
    made on its behalf — the default is silence about it, not assertion of it."""
    root, _ = fake_cgroup
    probe = oom.OomProbe.sample()
    _tree(root, "user.slice/user-1000.slice", 8)
    note = probe.describe(-signal.SIGKILL)
    assert note is not None and "oom_score_adj" not in note
