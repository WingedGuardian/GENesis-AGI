"""Cgroup evidence is correlation; missing/reset/replaced counters prove nothing."""

import pytest


@pytest.mark.parametrize("managed", [False, True])
def test_baseline_samples_only_the_declared_scope(tmp_path, managed):
    from genesis.cc import oom

    membership = tmp_path / "membership"
    membership.write_text("0::/user.slice/user-42.slice/user@42.service/app.slice/server.service\n")
    scope = tmp_path / "user.slice/user-42.slice"
    if not managed:
        scope /= "user@42.service/app.slice/server.service"
    scope.mkdir(parents=True)
    events = scope / "memory.events"
    events.write_text("oom 10\noom_kill 4\n")
    baseline = oom.capture_baseline(managed=managed, membership=membership, root=tmp_path, uid=42)
    events.write_text("oom 11\noom_kill 5\n")
    evidence = oom.capture_finish(baseline, -9)
    assert evidence.path == str(events)
    assert evidence.correlated
    assert evidence.before == 4 and evidence.after == 5


@pytest.mark.parametrize("after", [None, "oom_kill 4\n", "oom_kill 3\n", "oom 99\n", "oom_kill nope\n", "oom_kill -1\n"])
def test_absence_unchanged_reset_and_nonkill_events_are_not_attribution(tmp_path, after):
    from genesis.cc import oom

    events = tmp_path / "memory.events"
    events.write_text("oom_kill 4\n")
    baseline = oom._sample(events)
    if after is None:
        events.unlink()
    else:
        events.write_text(after)
    assert not oom.capture_finish(baseline, -9).correlated


def test_absence_at_baseline_is_not_a_zero_counter(tmp_path):
    from genesis.cc import oom

    events = tmp_path / "memory.events"
    baseline = oom._sample(events)
    events.write_text("oom_kill 999\n")
    evidence = oom.capture_finish(baseline, -9)
    assert evidence.before is None and not evidence.correlated


def test_replaced_counter_file_is_not_comparable(tmp_path):
    from genesis.cc import oom

    events = tmp_path / "memory.events"
    events.write_text("oom_kill 4\n")
    baseline = oom._sample(events)
    replacement = tmp_path / "replacement"
    replacement.write_text("oom_kill 999\n")
    replacement.replace(events)
    assert not oom.capture_finish(baseline, -9).correlated


@pytest.mark.parametrize("status", [None, 0, 1, 137, -15])
def test_only_proven_sigkill_correlates(status):
    from genesis.cc import oom

    assert not oom.OOMEvidence("synthetic", (1, 2), (1, 2), 4, 5, status).correlated


@pytest.mark.parametrize("membership_text", ["0::/other.slice/app.scope\n", "2:memory:/foo\n", "0::/user.slice/user-99.slice/a\n"])
def test_managed_launch_never_uses_arbitrary_ancestor(tmp_path, membership_text):
    from genesis.cc import oom

    membership = tmp_path / "membership"
    membership.write_text(membership_text)
    (tmp_path / "memory.events").write_text("oom_kill 99\n")
    baseline = oom.capture_baseline(managed=True, membership=membership, root=tmp_path, uid=42)
    assert baseline.path is None


def test_internal_termination_never_correlates():
    from genesis.cc import oom

    assert not oom.OOMEvidence("synthetic", (1, 2), (1, 2), 4, 5, -9, internal=True).correlated
