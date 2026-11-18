"""The Guardian's drop of the newest `live` candidate (#2978).

On `live` an approved REVERT_CODE drops the candidate check.py named before
Gate 2, through the engine's own `drop`, confirms through the engine that it is
gone, and only then restarts genesis-server. Every way the drop can end short
of that restarts nothing. The container is a fake that answers each command the
Guardian sends by what it contains."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from genesis.guardian.alert.dispatcher import AlertDispatcher
from genesis.guardian.config import GuardianConfig
from genesis.guardian.diagnosis import DiagnosisResult, RecoveryAction
from genesis.guardian.health_signals import HealthSnapshot, PauseState, SignalResult
from genesis.guardian.recovery import (
    RecoveryEngine,
    drop_target,
    live_candidates,
    update_lock_holder,
)
from genesis.guardian.snapshots import SnapshotManager
from genesis.guardian.state_machine import ConfirmationStateMachine

TIP = "a" * 40
HEAD_A = "1" * 40
HEAD_B = "2" * 40


def _view(
    listed=("feat/a", "feat/b"),
    holds=(("feat/a", HEAD_A), ("feat/b", HEAD_B)),
    tip=TIP,
    refusals=(),
    checkout=("refs/heads/live", TIP),
) -> dict:
    return {
        "version": 1,
        "manifest": {"state": "ok", "reason": None},
        "base": "b" * 40,
        "listed": [{"branch": b, "head": "0" * 40, "in_checkout": True} for b in listed],
        "live": {
            "tip": tip,
            "holds": None if holds is None else [{"branch": b, "head": h} for b, h in holds],
            "reason": None,
        },
        "checkout": None if checkout is None else {"branch_ref": checkout[0], "head": checkout[1]},
        "move_refusals": list(refusals),
    }


AFTER = _view(
    listed=("feat/a",),
    holds=(("feat/a", HEAD_A),),
    tip="c" * 40,
    checkout=("refs/heads/live", "c" * 40),
)


class Container:
    """Answers the Guardian's commands; ``views`` are the successive answers to
    `list --json --move-refusals` (the last one repeats)."""

    def __init__(self, views, drop=(0, "", ""), lock=(0, "", ""), marker=(1, "", ""), head=None):
        self.views = list(views)
        # ``lock``: one answer, or a list of successive answers (the last repeats).
        self.locks = list(lock) if isinstance(lock, list) else [lock]
        self.drop, self.marker, self.head = drop, marker, head
        self.calls: list[str] = []

    async def __call__(self, *args, timeout=10.0, **kwargs):
        # An exec's shell command, or the whole argv of a host-side incus call.
        cmd = args[-1] if args[1:2] == ("exec",) else " ".join(args)
        self.calls.append(cmd)
        if "list --json --move-refusals" in cmd:
            view = self.views.pop(0) if len(self.views) > 1 else self.views[0]
            return (0, "a login banner\n" + json.dumps(view), "")
        if "deploy_candidates drop" in cmd:
            return self.drop
        if "flock -n" in cmd:
            return self.locks.pop(0) if len(self.locks) > 1 else self.locks[0]
        if self.head is not None and "symbolic-ref" in cmd:
            return self.head
        if self.head is not None and "live_checkout.py" in cmd:
            return (1, "other\n", "")
        if "update_in_progress.pid" in cmd:
            return self.marker
        return (0, "", "")  # systemctl, journalctl

    @property
    def restarted(self) -> bool:
        return any("systemctl --user" in c and ("restart" in c or "start" in c) for c in self.calls)

    @property
    def dropped(self) -> list[str]:
        return [c for c in self.calls if "deploy_candidates drop" in c]


@pytest.fixture
def engine() -> RecoveryEngine:
    config = GuardianConfig()
    d = AlertDispatcher()
    ch = AsyncMock()
    ch.send.return_value = True
    d.add_channel(ch)
    return RecoveryEngine(config, ConfirmationStateMachine(config), SnapshotManager(config), d)


@pytest.fixture(autouse=True)
def unit_comes_up():
    seen = []

    async def fake(container):
        seen.append(container)
        return {"ActiveState": "active", "SubState": "running", "InvocationID": str(len(seen))}

    with (
        patch("genesis.guardian.recovery._server_unit", fake),
        patch("asyncio.sleep", new_callable=AsyncMock),
    ):
        yield


def _box(box):
    """Both hops into the container: recovery's own and live_drop's reads."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("genesis.guardian.recovery._run_subprocess", box))
    stack.enter_context(patch("genesis.guardian.live_drop._run_subprocess", box))
    return stack


async def _drop(engine, box, target=("feat/b", HEAD_B, TIP)):
    with _box(box):
        return await engine._revert_code("genesis", target)


# ── choosing the candidate ───────────────────────────────────────────────────


def test_the_newest_is_the_last_held_candidate_still_listed():
    assert drop_target(_view()) == (("feat/b", HEAD_B, TIP), "")


def test_a_half_done_drop_is_finished_by_a_rebuild_never_by_the_next_drop():
    """A drop that saved the manifest and could not move leaves `live` holding a
    branch the manifest no longer lists (architect S1)."""
    target, why = drop_target(_view(listed=("feat/a",)))
    assert target is None
    assert "feat/b" in why and "rebuild" in why


@pytest.mark.parametrize(
    ("view", "said"),
    [
        (_view(refusals=("tree is dirty",)), "would refuse: tree is dirty"),
        (_view(checkout=("refs/heads/main", TIP)), "not on `live`"),
        (_view(checkout=("refs/heads/live", "d" * 40)), "not on `live` at its tip"),
        (_view(checkout=None), "not on `live`"),
        (_view(holds=None), "could not be read"),
        (_view(holds=()), "holds no candidate"),
        (_view(tip=None), "could not be read"),
        (
            _view(listed=("feat/a", "-x y"), holds=(("feat/a", HEAD_A), ("-x y", HEAD_B))),
            "does not accept",
        ),
        (_view(holds=(("feat/a", HEAD_A), ("feat/b", "not-a-commit"))), "does not accept"),
    ],
    ids=[
        "refusal",
        "off-live",
        "not-at-tip",
        "no-checkout",
        "holds-unread",
        "none-held",
        "no-tip",
        "bad-name",
        "bad-head",
    ],
)
def test_no_drop_is_proposed_when_the_engine_says_it_cannot_run(view, said):
    target, why = drop_target(view)
    assert target is None and said in why


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        (1, "", "boom"),
        (0, "", ""),
        (0, "not json", ""),
        (0, json.dumps({"version": 2}), ""),
        (0, json.dumps({**_view(), "move_refusals": "x"}), ""),
    ],
    ids=["exit-1", "empty", "not-json", "version", "refusals-not-a-list"],
)
async def test_an_unreadable_list_is_none(answer):
    async def box(*args, **kwargs):
        return answer

    with _box(box):
        assert await live_candidates("genesis") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lock", "marker", "want"),
    [
        ((0, "", ""), (0, "4242", ""), (False, "")),
        ((75, "", ""), (0, "4242\n", ""), (True, "the deploy marker names pid 4242")),
        ((75, "", ""), (1, "", "No such file"), (True, "")),
        ((66, "", "cannot open lock file"), (0, "4242", ""), (None, "")),
        ((-1, "", "timeout"), (0, "4242", ""), (None, "")),
    ],
    ids=["free", "held-named", "held-unnamed", "no-lock-dir", "timeout"],
)
async def test_the_lock_probe(lock, marker, want):
    box = Container([_view()], lock=lock, marker=marker)
    with _box(box):
        assert await update_lock_holder("genesis") == want


# ── running it ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_confirmed_drop_restarts(engine):
    box = Container([_view(), AFTER], drop=(0, "  NOTE: feat/c left `live` with it", ""))
    ok, detail = await _drop(engine, box)
    assert ok is True, detail
    assert box.dropped == [
        "cd ~/genesis && scripts/deploy_candidates drop feat/b --expect-branch live "
        f"--expect-head {TIP} --wait 120"
    ]
    assert box.restarted
    assert "dropped feat/b" in detail and "feat/c left `live`" in detail


@pytest.mark.asyncio
async def test_notes_past_the_third_are_counted_not_dropped(engine):
    """Class audit (round 2): the engine's NOTE lines name candidates that left
    `live` with the drop; past three, the detail says how many it left out."""
    notes = "\n".join(f"  NOTE: feat/n{i} left `live` with it" for i in range(5))
    box = Container([_view(), AFTER], drop=(0, notes, ""))
    ok, detail = await _drop(engine, box)
    assert ok is True and "feat/n2" in detail and "feat/n3" not in detail, detail
    assert "2 more NOTE line(s)" in detail, detail


@pytest.mark.asyncio
async def test_the_branch_is_quoted_for_the_shell(engine):
    view = _view(listed=("feat/a", "feat/b;$x"), holds=(("feat/a", HEAD_A), ("feat/b;$x", HEAD_B)))
    after = _view(listed=("feat/a",), holds=(("feat/a", HEAD_A),))
    box = Container([view, after])
    ok, _ = await _drop(engine, box, ("feat/b;$x", HEAD_B, TIP))
    assert ok is True
    assert "drop 'feat/b;$x' --expect-branch" in box.dropped[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("drop", "said"),
    [
        ((200, "", "ERROR: update.lock still held"), "update.lock was held for 120 s"),
        (
            (1, "", "ERROR: first line\nmiddle\nThe manifest change WAS saved"),
            "first line … The manifest change WAS saved",
        ),
        ((-1, "", "No such file or directory: incus"), "No such file or directory: incus"),
    ],
    ids=["lock-held", "refused", "exec-error"],
)
async def test_a_drop_that_did_not_finish_restarts_nothing(engine, drop, said):
    box = Container([_view()], drop=drop)
    ok, detail = await _drop(engine, box)
    assert ok is False
    assert said in detail and "nothing restarted" in detail
    assert not box.restarted


@pytest.mark.asyncio
async def test_a_drop_reporting_success_that_the_engine_does_not_confirm_restarts_nothing(engine):
    box = Container([_view(), _view()])  # still listed and held after "rc 0"
    ok, detail = await _drop(engine, box)
    assert ok is False and "does not show it gone" in detail
    assert not box.restarted


@pytest.mark.asyncio
async def test_a_dirty_tree_after_the_drop_restarts_nothing(engine):
    """A half-moved tree reads dirty: the engine's move refusal says so."""
    dirty = {**AFTER, "move_refusals": ["uncommitted tracked changes"]}
    box = Container([_view(), dirty])
    ok, detail = await _drop(engine, box)
    assert ok is False and "uncommitted tracked changes" in detail
    assert not box.restarted


@pytest.mark.asyncio
async def test_a_client_timeout_waits_for_the_drop_then_restarts(engine):
    """incus exec keeps running the drop after the client is killed (MEASURED
    2026-10-07): the Guardian waits for the engine to show it gone."""
    box = Container(
        [_view(), AFTER],
        drop=(-1, "", "timeout"),
        lock=[(75, "", ""), (75, "", ""), (0, "", "")],
    )
    ok, detail = await _drop(engine, box)
    assert ok is True, detail
    assert box.restarted


@pytest.mark.asyncio
async def test_a_drop_still_running_at_the_end_restarts_nothing(engine):
    box = Container([_view()], drop=(-1, "", "timeout"), lock=(75, "", ""))
    ok, detail = await _drop(engine, box)
    assert ok is False and "had not finished" in detail
    assert not box.restarted


@pytest.mark.asyncio
async def test_a_timeout_whose_lock_is_free_without_the_drop_restarts_nothing(engine):
    box = Container([_view()], drop=(-1, "", "timeout"), lock=(0, "", ""))
    ok, detail = await _drop(engine, box)
    assert ok is False and "lock is free" in detail
    assert not box.restarted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "now",
    [
        _view(
            listed=("feat/a", "feat/b", "feat/c"),
            holds=(("feat/a", HEAD_A), ("feat/b", HEAD_B), ("feat/c", "3" * 40)),
        ),
        _view(holds=(("feat/a", HEAD_A), ("feat/b", "4" * 40))),
        _view(tip="e" * 40, checkout=("refs/heads/live", "e" * 40)),
        _view(refusals=("an update is in progress",)),
    ],
    ids=["newer-candidate", "re-pinned", "tip-moved", "refusal"],
)
async def test_a_changed_candidate_set_runs_no_drop(engine, now):
    box = Container([now])
    ok, detail = await _drop(engine, box)
    assert ok is False and "changed since approval" in detail
    assert box.dropped == [] and not box.restarted


@pytest.mark.asyncio
async def test_an_approved_drop_never_falls_through_to_a_revert(engine):
    """The checkout left `live` after approval: the drop refuses; no stash, no
    revert. Both probes say `main`, so only the approved drop's own refusal can
    stop the revert here (architect review: an empty probe read as `live`)."""
    box = Container([_view(checkout=("refs/heads/main", TIP))], head=(0, "refs/heads/main\n", ""))
    ok, detail = await _drop(engine, box)
    assert ok is False and "changed since approval" in detail
    assert not any("git stash" in c or "git revert" in c for c in box.calls)


@pytest.mark.asyncio
async def test_a_lock_taken_after_the_drop_restarts_nothing(engine):
    """A rebuild queued behind the drop takes update.lock as soon as it exits."""
    box = Container([_view(), AFTER], lock=(75, "", ""))
    ok, detail = await _drop(engine, box)
    assert ok is False and "update.lock is held on `live`" in detail
    assert not box.restarted
    assert engine._sm.state.waiting_on_update_lock is True  # it resumes, not goes quiet


@pytest.mark.asyncio
async def test_a_lock_taken_after_the_drop_and_released_restarts(engine):
    box = Container([_view(), AFTER], lock=[(75, "", ""), (75, "", ""), (0, "", "")])
    ok, detail = await _drop(engine, box)
    assert ok is True, detail
    assert box.restarted


@pytest.mark.asyncio
async def test_a_drop_that_finishes_right_after_the_timeout_is_seen(engine):
    """The poll reads the lock first and the engine's view after it: a drop that
    ends between the two reads is not reported "not gone" (rework review C)."""
    box = Container([_view(), AFTER], drop=(-1, "", "timeout"), lock=(0, "", ""))
    ok, detail = await _drop(engine, box)
    assert ok is True, detail
    assert box.restarted


@pytest.mark.asyncio
async def test_a_drop_kept_out_by_the_lock_marks_the_wait(engine):
    box = Container([_view()], drop=(200, "", "ERROR: still held"))
    ok, _ = await _drop(engine, box)
    assert ok is False and not box.restarted
    assert engine._sm.state.waiting_on_update_lock is True


@pytest.mark.asyncio
async def test_a_timed_out_drop_that_is_gone_but_still_holds_the_lock_restarts_nothing(engine):
    """The drop's tail (hook sync) still runs under the lock after `live` moved."""
    box = Container([_view(), AFTER], drop=(-1, "", "timeout"), lock=(75, "", ""))
    ok, detail = await _drop(engine, box)
    assert ok is False and "had not finished" in detail
    assert not box.restarted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lock", "head", "restarts"),
    [
        ((75, "", ""), (0, "refs/heads/live\n", ""), False),
        ((75, "", ""), (0, "refs/heads/main\n", ""), True),
        ((0, "", ""), (0, "refs/heads/live\n", ""), True),
        ((-1, "", "timeout"), (0, "refs/heads/live\n", ""), True),
    ],
    ids=["live-held", "main-held", "live-free", "probe-failed"],
)
async def test_a_container_restart_on_live_waits_for_the_lock(engine, lock, head, restarts):
    """The approval may be hours old: an approved RESTART_CONTAINER re-reads the
    lock and refuses on `live` while it is held."""
    box = Container([_view()], lock=lock, head=head)
    with _box(box):
        ok, detail = await engine._restart_container("genesis")
    assert any(c.startswith("incus restart") for c in box.calls) is restarts, (detail, box.calls)
    assert engine._sm.state.waiting_on_update_lock is (not restarts)


def test_container_strings_are_cleaned_and_cut():
    from genesis.guardian.live_drop import shown

    assert shown("a\nProposed: <b>X</b>\u2028b") == "a Proposed: <b>X</b> b"
    assert shown("x" * 500).endswith(" (cut)") and len(shown("x" * 500)) == 206


@pytest.mark.asyncio
async def test_deeply_nested_json_is_unreadable_not_a_crash():
    async def box(*args, **kwargs):
        return (0, "[" * 100_000, "")

    with _box(box):
        assert await live_candidates("genesis") is None


@pytest.mark.asyncio
async def test_execute_hands_the_diagnosis_candidate_to_the_drop(engine):
    diagnosis = DiagnosisResult(
        likely_cause="bad candidate",
        confidence_pct=80,
        evidence=[],
        recommended_action=RecoveryAction.REVERT_CODE,
        drop_candidate=("feat/b", HEAD_B, TIP),
    )
    healthy = HealthSnapshot(
        signals={n: SignalResult(n, True, 1.0, "ok", "t") for n in ["health_api"]},
        pause_state=PauseState(paused=False),
    )
    box = Container([_view(), AFTER])
    with (
        _box(box),
        patch("genesis.guardian.recovery.collect_all_signals", return_value=healthy),
        patch.object(engine._snapshots, "take", return_value="pre"),
        patch.object(engine._snapshots, "safe_to_snapshot", AsyncMock(return_value=False)),
    ):
        result = await engine.execute(diagnosis)
    assert result.success is True, result.detail
    assert len(box.dropped) == 1 and box.restarted


@pytest.mark.asyncio
async def test_the_drop_is_judged_again_after_waiting_for_the_lock(engine):
    """Review r1 (Devin): a rebuild queued behind the drop can run during the
    wait and leave the tree half-moved; the restart reads the engine again."""
    dirty = {**AFTER, "move_refusals": ["uncommitted tracked changes"]}
    box = Container([_view(), AFTER, dirty], lock=[(75, "", ""), (0, "", "")])
    ok, detail = await _drop(engine, box)
    assert ok is False and "after waiting for update.lock" in detail
    assert "uncommitted tracked changes" in detail
    assert not box.restarted


def _drop_diagnosis():
    return DiagnosisResult(
        likely_cause="bad candidate",
        confidence_pct=80,
        evidence=[],
        recommended_action=RecoveryAction.REVERT_CODE,
        drop_candidate=("feat/b", HEAD_B, TIP),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("drop", "deferred"),
    [((200, "", "ERROR: still held"), True), ((1, "", "ERROR: refused"), False)],
    ids=["lock-held", "refused-control"],
)
async def test_a_lock_deferral_costs_no_recovery_attempt(engine, drop, deferred):
    """Review r1 (Devin): an action held back by update.lock ran nothing, so it
    is no attempt; the episode resumes with its budget once the lock is free.
    A real failure still counts (the control)."""
    from genesis.guardian.state_machine import GuardianState

    box = Container([_view()], drop=drop)
    before = engine._sm.state.recovery_attempts
    send = AsyncMock()
    with (
        _box(box),
        patch.object(engine._snapshots, "safe_to_snapshot", AsyncMock(return_value=False)),
        patch.object(engine._dispatcher, "send", send),
    ):
        result = await engine.execute(_drop_diagnosis())
    assert result.success is False
    assert (engine._sm.state.recovery_attempts == before) is deferred
    assert engine._sm.state.current_state == GuardianState.CONFIRMED_DEAD
    assert engine._sm.state.waiting_on_update_lock is deferred
    last = send.await_args_list[-1].args[0].title
    assert last.startswith("Recovery deferred") is deferred
    assert last.startswith("Recovery failed") is (not deferred)
