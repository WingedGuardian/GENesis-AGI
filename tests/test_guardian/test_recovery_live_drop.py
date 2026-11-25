"""The Guardian runs an approved drop of the newest `live` candidate (#2978).

With ``recovery.live_engine`` on, an approved REVERT_CODE on `live` is ONE call
to the engine's guarded drop (`drop <branch> --expect-branch live --expect-head
<tip> --expect-candidate <pinned> --expect-leaving <json> --restart-server --wait
0`), which drops,
confirms and queues the restart under one update.lock hold. The Guardian only
judges the outcome: the action succeeds when genesis-server booted from
`live`'s new tip (scripts/lib/serving_commit.py, as deploy_code_only.sh status
reads it), with no 30 s probe. Every exit code has a row here. Off (the
default), recovery refuses REVERT_CODE on `live` as before. The container is a
fake that answers each command the Guardian sends by what it contains."""

from __future__ import annotations

import asyncio
import json
import logging
import shlex
from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.guardian.alert.dispatcher import AlertDispatcher
from genesis.guardian.config import GuardianConfig
from genesis.guardian.diagnosis import DiagnosisResult, RecoveryAction
from genesis.guardian.recovery import _DROP_TIMEOUT_S, RecoveryEngine
from genesis.guardian.snapshots import SnapshotManager
from genesis.guardian.state_machine import ConfirmationStateMachine, GuardianState

TIP = "a" * 40
NEW_TIP = "c" * 40
HEAD_A, HEAD_B = "1" * 40, "2" * 40
PIN_A, PIN_B = "3" * 40, "4" * 40
TARGET = ("feat/b", PIN_B, HEAD_B, TIP, ())


def _view(listed, holds, tip, checkout_head=None) -> dict:
    return {
        "version": 1,
        "manifest": {"state": "ok", "reason": None},
        "base": "b" * 40,
        "listed": [{"branch": b, "head": h, "in_checkout": True} for b, h in listed],
        "live": {"tip": tip, "holds": [{"branch": b, "head": h} for b, h in holds], "reason": None},
        "checkout": {"branch_ref": "refs/heads/live", "head": checkout_head or tip},
        "move_refusals": [],
    }


BEFORE = _view(
    (("feat/a", PIN_A), ("feat/b", PIN_B)), (("feat/a", HEAD_A), ("feat/b", HEAD_B)), TIP
)
AFTER = _view((("feat/a", PIN_A),), (("feat/a", HEAD_A),), NEW_TIP)
#: The manifest saved without feat/b, `live` not moved: a half-done drop.
HALF = _view((("feat/a", PIN_A),), (("feat/a", HEAD_A), ("feat/b", HEAD_B)), TIP)


def _boot(boot: str, live: str = NEW_TIP, state: str = "active", after: str = "x") -> str:
    return (
        f"a login banner\nActiveState={state}\nSubState=running\nInvocationID=x\nResult=success\n"
        f"live={live}\nboot={boot}\nafter={after}"
    )


#: genesis-server as it was before the drop: booted from `live`'s old tip.
OLD = _boot(TIP, TIP)
#: booted from `live`'s new tip: the drop took.
NEW = _boot(NEW_TIP)


class Box:
    """Answers the Guardian's commands. ``boots``: the successive answers of
    the server-boot probe (the last repeats) until the Guardian itself restarts
    genesis-server; ``after_restart`` from then on."""

    def __init__(self, drop=(0, "", ""), boots=(OLD, NEW), views=(AFTER,), after_restart=NEW):
        self.drop, self.after_restart = drop, after_restart
        self.boots, self.views = list(boots), list(views)
        self.calls: list[str] = []
        self.drop_timeout: float | None = None
        self.restarts = 0  # the Guardian's own starts and restarts

    async def __call__(self, *args, timeout=10.0, **kwargs):
        cmd = args[-1] if args[1:2] == ("exec",) else " ".join(args)
        self.calls.append(cmd)
        if "deploy_candidates drop" in cmd:
            self.drop_timeout = timeout
            return self.drop
        if "list --json --move-refusals" in cmd:
            view = self.views.pop(0) if len(self.views) > 1 else self.views[0]
            return (1, "", "") if view is None else (0, "a banner\n" + json.dumps(view), "")
        if "serving_commit.py" in cmd:
            if self.restarts:
                answer = self.after_restart
            else:
                answer = self.boots.pop(0) if len(self.boots) > 1 else self.boots[0]
            return (0, answer, "") if isinstance(answer, str) else answer
        if "--no-block genesis-server" in cmd:
            self.restarts += 1
            return (0, "", "")
        if "systemctl --user show genesis-server" in cmd:
            return (0, f"ActiveState=active\nSubState=running\nInvocationID=i{self.restarts}", "")
        if "symbolic-ref" in cmd:
            return (0, "refs/heads/live\n", "")
        if "live_checkout.py" in cmd:
            return (0, "live\n", "")
        return (0, "", "")  # journalctl

    @property
    def dropped(self) -> list[str]:
        return [c for c in self.calls if "deploy_candidates drop" in c]


def _engine(armed: bool = True) -> RecoveryEngine:
    config = GuardianConfig()
    config.recovery.live_engine = armed
    d = AlertDispatcher()
    ch = AsyncMock()
    ch.send.return_value = True
    d.add_channel(ch)
    return RecoveryEngine(config, ConfirmationStateMachine(config), SnapshotManager(config), d)


@pytest.fixture(autouse=True)
def _no_sleep():
    with patch("asyncio.sleep", new_callable=AsyncMock):
        yield


def _patched(box):
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(patch("genesis.guardian.recovery._run_subprocess", box))
    stack.enter_context(patch("genesis.guardian.live_drop._run_subprocess", box))
    return stack


async def _drop(box, engine=None, target=TARGET):
    engine = engine or _engine()
    engine._mark_drop_attempt(target)  # execute() records it before the call (#5007)
    with _patched(box):
        ok, detail = await engine._drop_candidate("genesis", *target)
    return ok, detail, engine


def _attempt_set(engine) -> bool:
    rec = engine._sm.state.live_drop_attempt
    return rec is not None and rec["branch"] == "feat/b" and rec["pinned_head"] == PIN_B


# ── the call ─────────────────────────────────────────────────────────────────


async def test_the_drop_is_one_call_to_the_engines_guarded_form():
    box = Box()
    await _drop(box)
    assert box.dropped == [
        "cd ~/genesis && scripts/deploy_candidates drop feat/b --expect-branch live "
        f"--expect-head {TIP} --expect-candidate {PIN_B} --expect-leaving '[]' "
        "--restart-server --wait 0"
    ]
    assert box.drop_timeout == _DROP_TIMEOUT_S == 120.0


async def test_the_drop_passes_the_leaving_set_gate_2_showed():
    """The engine refuses, nothing changed, when its plan takes out another set."""
    box = Box()
    await _drop(box, target=(*TARGET[:4], ("feat/a", "feat/it's")))
    assert "--expect-leaving " + shlex.quote(json.dumps(["feat/a", "feat/it's"])) in box.dropped[0]


async def test_a_branch_name_reaches_the_shell_quoted():
    box = Box()
    await _drop(box, target=("feat/it's", PIN_B, HEAD_B, TIP, ()))
    assert f"drop {shlex.quote(chr(39).join(['feat/it', 's']))} --expect-branch" in box.dropped[0]


async def test_note_lines_are_kept_three_and_counted():
    out = "\n".join(f"  NOTE: feat/x{i} left `live` with it" for i in range(5))
    ok, detail, _ = await _drop(Box(drop=(0, out, "")))
    assert ok and detail.count("NOTE:") == 3 and "and 2 more NOTE line(s)" in detail


# ── exit 0 ───────────────────────────────────────────────────────────────────


async def test_exit_0_succeeds_when_the_server_booted_from_the_new_tip():
    box = Box(drop=(0, "  genesis-server: restart queued.", ""))
    ok, detail, engine = await _drop(box)
    assert ok, detail
    assert "dropped feat/b" in detail and NEW_TIP[:12] in detail
    assert box.restarts == 0  # the engine queued it


async def test_exit_0_without_a_boot_from_the_new_tip_fails_and_records_the_attempt():
    box = Box(boots=(OLD,))
    ok, detail, engine = await _drop(box)
    assert not ok and "has not booted from `live`'s new tip" in detail
    assert box.restarts == 0 and _attempt_set(engine)


async def test_a_restart_during_the_boot_probe_is_not_a_boot_from_the_new_tip():
    """Round 3: a restart between the probe reads pairs the state of one run
    with the boot of the next, so a changed InvocationID reads as unknown."""
    box = Box(boots=(OLD, _boot(NEW_TIP, after="y")))
    ok, detail, engine = await _drop(box)
    assert not ok and "restarted during the probe" in detail
    assert _attempt_set(engine)


async def test_exit_0_with_an_unknown_boot_commit_is_not_success():
    box = Box(boots=(OLD, _boot("unknown: HEAD moved in the same second the server booted")))
    ok, detail, _ = await _drop(box)
    assert not ok and "same second" in detail


async def test_exit_0_a_crash_loop_on_the_old_tree_is_not_success():
    """A new invocation alone proves nothing (Restart=on-failure changes it):
    the boot commit must be `live`'s new tip."""
    box = Box(boots=(OLD, _boot(TIP, NEW_TIP)))
    ok, _, _ = await _drop(box)
    assert not ok


async def test_an_approved_drop_skips_the_30s_probe_and_leaves_health_to_the_next_check():
    engine = _engine()
    diagnosis = DiagnosisResult(
        likely_cause="crash loop",
        confidence_pct=80,
        evidence=[],
        recommended_action=RecoveryAction.REVERT_CODE,
        drop_candidate=TARGET,
    )
    probes = AsyncMock()
    with _patched(Box()), patch("genesis.guardian.recovery.collect_all_signals", probes):
        result = await engine.execute(diagnosis)
    assert result.success and "dropped feat/b" in result.detail
    probes.assert_not_awaited()
    assert engine._sm.current_state == GuardianState.RECOVERED


def _drop_diagnosis(action=RecoveryAction.REVERT_CODE) -> DiagnosisResult:
    return DiagnosisResult(
        likely_cause="crash loop",
        confidence_pct=80,
        evidence=[],
        recommended_action=action,
        drop_candidate=TARGET,
    )


@pytest.mark.parametrize(
    "drop,state",
    [
        ((-1, "", "timeout"), GuardianState.RECOVERED),  # an attempt is recorded
        ((1, "", "ERROR: x\nThe manifest change WAS saved"), GuardianState.RECOVERED),
    ],  # exit 200 is a lock wait (B2): test_drop_exit_200_and_the_deploy_marker_are_waits…
)
async def test_an_action_that_ran_after_a_drop_attempt_ends_in_recovered(drop, state):
    """Round-2 premise check, class A: RECOVERED's one transition to
    CONFIRMED_DEAD is what reopens the next diagnosis past the latch."""
    engine = _engine()
    sent = AsyncMock()
    with _patched(Box(drop=drop, views=(HALF,))), patch.object(engine._dispatcher, "send", sent):
        result = await engine.execute(_drop_diagnosis())
    assert not result.success and engine._sm.current_state == state
    assert sent.await_args.args[0].title == "Recovery failed: REVERT_CODE"  # alerts unchanged


async def test_a_restart_after_a_drop_attempt_that_leaves_genesis_down_is_judged_again():
    """The RESTART_SERVICES the proposal offers after a drop can boot the new tip
    and fail its probes: it must reach the next diagnosis too."""
    engine = _engine()
    engine._sm.state.live_drop_attempt = {"branch": "feat/b", "pinned_head": PIN_B, "tip": TIP, "at": "t"}
    probes = AsyncMock(return_value=MagicMock(all_alive=False, failed_signals=[]))
    with _patched(Box()), patch("genesis.guardian.recovery.collect_all_signals", probes):
        result = await engine.execute(_drop_diagnosis(RecoveryAction.RESTART_SERVICES))
    assert not result.success and engine._sm.current_state == GuardianState.RECOVERED


async def test_a_deferred_action_says_so_in_its_result():
    engine = _engine()
    engine._sm.record_recovery_attempt("RESTART_SERVICES")
    result = await engine.execute(_drop_diagnosis(RecoveryAction.RESTART_SERVICES))
    assert result.deferred and "backoff" in result.detail and "remaining" in result.detail


async def test_empty_tip_and_boot_reads_are_not_a_boot_from_the_new_tip():
    """A failed `live` read on a stopped unit gives two empty strings."""
    unread = _boot("unknown: x", live="", state="failed")
    ok, detail, _ = await _drop(Box(boots=(unread,)))
    assert not ok and "has not booted" in detail


# ── exit 4 and 5 ─────────────────────────────────────────────────────────────


async def test_exit_4_restarts_once_in_the_same_action():
    box = Box(drop=(4, "", "ERROR: the drop is done and confirmed, but ... exited 1"), boots=(OLD,))
    ok, detail, _ = await _drop(box)
    assert ok, detail
    assert box.restarts == 1


async def test_exit_5_with_the_new_boot_seen_restarts_nothing():
    ok, _, _ = await _drop(Box(drop=(5, "", "ERROR: ... got no answer")))
    assert ok


async def test_exit_5_with_the_old_server_still_running_restarts_once():
    box = Box(drop=(5, "", "ERROR: ... got no answer"), boots=(OLD, _boot(TIP)))
    ok, detail, _ = await _drop(box)
    assert ok, detail
    assert box.restarts == 1


async def test_exit_5_while_the_unit_is_activating_restarts_nothing():
    box = Box(drop=(5, "", ""), boots=(OLD, _boot("unknown: x", state="activating")))
    ok, detail, engine = await _drop(box)
    assert not ok and box.restarts == 0
    assert "restart outcome unknown" in detail and _attempt_set(engine)


async def test_exit_5_with_an_unknown_boot_commit_restarts_nothing():
    box = Box(drop=(5, "", ""), boots=(OLD, _boot("unknown: the reflog is empty")))
    ok, detail, _ = await _drop(box)
    assert not ok and box.restarts == 0 and "restart outcome unknown" in detail


async def test_exit_5_with_an_unreadable_unit_restarts_nothing():
    box = Box(drop=(5, "", ""), boots=(OLD, (1, "", "")))
    ok, _, _ = await _drop(box)
    assert not ok and box.restarts == 0


# ── exit 1 ───────────────────────────────────────────────────────────────────


async def test_exit_1_refused_changes_nothing_and_records_no_attempt():
    err = f"ERROR: the manifest pins feat/b at {'5' * 12}, not {PIN_B[:12]} (--expect-candidate): it changed since that was read; nothing changed."
    box = Box(drop=(1, "", err), views=(BEFORE,))
    ok, detail, engine = await _drop(box)
    assert not ok and "it changed since that was read" in detail
    assert box.restarts == 0 and engine._sm.state.live_drop_attempt is None


async def test_exit_1_after_the_save_with_the_drop_done_restarts_once():
    """The engine's still_in=False text: the drop is done, the restart missing."""
    err = (
        "ERROR: the drop of feat/b could not be confirmed: x. The manifest change WAS saved "
        "(feat/b is no longer a candidate) and the restart was not queued: the drop itself is "
        "done (feat/b is out of `live`); fix the above, then take the restart step printed above."
    )
    box = Box(drop=(1, "", err), boots=(OLD,))
    ok, detail, _ = await _drop(box)
    assert ok, detail
    assert box.restarts == 1


async def test_exit_1_after_the_save_half_done_restarts_nothing_and_names_rebuild():
    err = "ERROR: x\nThe manifest change WAS saved (feat/b is no longer a candidate), but `live` still runs it: fix the above, then run scripts/deploy_candidates rebuild."
    box = Box(drop=(1, "", err), views=(HALF,))
    ok, detail, engine = await _drop(box)
    assert not ok and box.restarts == 0
    assert "deploy_candidates rebuild" in detail and _attempt_set(engine)


async def test_exit_1_after_the_save_with_an_unreadable_view_restarts_nothing():
    err = "ERROR: the drop of feat/b could not be confirmed: y. The manifest change WAS saved"
    box = Box(drop=(1, "", err), views=(None,))
    ok, _, engine = await _drop(box)
    assert not ok and box.restarts == 0 and _attempt_set(engine)


async def test_exit_1_while_a_deploy_holds_the_marker_is_a_wait_not_a_fault():
    err = "ERROR: a live deploy (pid 5) holds the deploy marker /x — a dashboard update or a restore is running. Nothing changed."
    ok, detail, engine = await _drop(Box(drop=(1, "", err), views=(BEFORE,)))
    assert not ok and "a deploy is running" in detail
    assert engine._sm.state.live_drop_attempt is None


@pytest.mark.parametrize(
    "view",
    [
        None,  # unreadable
        HALF,  # the manifest changed
        _view(
            (("feat/a", PIN_A), ("feat/b", PIN_B)),
            (("feat/a", HEAD_A), ("feat/b", HEAD_B)),
            TIP,
            checkout_head="d" * 40,
        ),  # mid-move
    ],
)
async def test_an_exit_1_without_a_named_outcome_is_not_called_nothing_changed(view):
    """An incus failure or an unexpected engine error also exits 1: "nothing
    changed" needs a fresh view showing the pin, `live` and the checkout as they were."""
    box = Box(drop=(1, "", "Error: websocket: close 1006"), views=(view,))
    ok, detail, engine = await _drop(box)
    assert not ok and "nothing changed" not in detail
    assert box.restarts == 0 and _attempt_set(engine)


@pytest.mark.parametrize(
    "said",
    [
        "ERROR: the checkout is on main, not live at the expected commit; nothing changed.",
        "ERROR: files are in the way. Nothing changed.",
        "ERROR: x\nThe manifest is unchanged: this drop wrote nothing and `live` did not move.",
    ],
)
async def test_the_engines_own_refusal_records_no_attempt_on_an_unsettled_checkout(said):
    """A checkout off `live` is why the engine refused: an attempt recorded here
    would hold every later proposal at "may still be running"."""
    off_live = {**BEFORE, "checkout": {"branch_ref": "refs/heads/main", "head": "d" * 40}}
    box = Box(drop=(1, "", said), views=(off_live,))
    ok, detail, engine = await _drop(box)
    assert not ok and "was refused, nothing changed" in detail
    assert box.restarts == 0 and engine._sm.state.live_drop_attempt is None


async def test_exit_1_after_the_save_with_a_remaining_refusal_restarts_nothing():
    err = "ERROR: the drop of feat/b could not be confirmed: x. The manifest change WAS saved"
    view = {**AFTER, "move_refusals": ["the tree has uncommitted changes"]}
    box = Box(drop=(1, "", err), boots=(OLD,), views=(view,))
    ok, _, engine = await _drop(box)
    assert not ok and box.restarts == 0 and _attempt_set(engine)


# ── the other codes ──────────────────────────────────────────────────────────


async def test_exit_2_is_a_guardian_bug(caplog):
    with caplog.at_level(logging.ERROR):
        ok, detail, engine = await _drop(Box(drop=(2, "", "usage: ...")))
    assert not ok and "Guardian bug" in detail and "usage" in caplog.text
    assert engine._sm.state.live_drop_attempt is None


async def test_exit_97_is_the_test_fence():
    ok, detail, _ = await _drop(Box(drop=(97, "", "fence")))
    assert not ok and "test fence" in detail


async def test_exit_200_did_nothing():
    ok, detail, engine = await _drop(Box(drop=(200, "", "")))
    assert not ok and "update.lock is held" in detail and "nothing was done" in detail
    assert engine._sm.state.live_drop_attempt is None


@pytest.mark.parametrize("rc", [130, 143])
async def test_a_killed_drop_records_the_attempt_and_restarts_nothing(rc):
    box = Box(drop=(rc, "", ""), views=(HALF,))
    ok, detail, engine = await _drop(box)
    assert not ok and box.restarts == 0 and _attempt_set(engine)
    assert "deploy_candidates rebuild" in detail


async def test_a_client_timeout_records_the_attempt_and_restarts_nothing():
    box = Box(drop=(-1, "", "timeout"))
    ok, detail, engine = await _drop(box)
    assert not ok and "outcome was not seen" in detail
    assert box.restarts == 0 and _attempt_set(engine) and len(box.dropped) == 1


@pytest.mark.parametrize(
    "drop", [(126, "", ""), (127, "", ""), (255, "", ""), (-1, "", "No such file")]
)
async def test_an_unknown_exit_is_judged_like_a_timeout(drop):
    box = Box(drop=drop)
    ok, detail, engine = await _drop(box)
    assert not ok and "outcome was not seen" in detail
    assert box.restarts == 0 and _attempt_set(engine)


# ── off (the default): main's behaviour ──────────────────────────────────────


async def test_flag_off_recovery_refuses_revert_code_on_live_even_with_a_candidate():
    engine = _engine(armed=False)
    diagnosis = replace(
        DiagnosisResult(
            likely_cause="x",
            confidence_pct=80,
            evidence=[],
            recommended_action=RecoveryAction.REVERT_CODE,
        ),
        drop_candidate=TARGET,
    )
    box = Box()
    with _patched(box):
        ok, detail = await engine._execute_action(RecoveryAction.REVERT_CODE, diagnosis)
    assert not ok and detail.startswith("REVERT_CODE refused: the container checkout is on `live`")
    assert "ends lock holders" in detail  # main's text, unchanged
    assert not box.dropped


async def test_a_half_done_drop_escalation_names_the_rebuild():
    engine = _engine()
    diagnosis = DiagnosisResult(
        likely_cause="`live` still runs feat/b",
        confidence_pct=80,
        evidence=[],
        recommended_action=RecoveryAction.ESCALATE,
        escalation="half_done_drop",
    )
    sent = AsyncMock()
    with patch.object(engine._dispatcher, "send", sent):
        result = await engine.execute(diagnosis)
    alert = sent.await_args.args[0]
    assert result.success and "a drop did not finish" in alert.title
    assert "deploy_candidates rebuild" in alert.body


# ── #5007: the outcome-unknown end state is on disk before the engine call ───


def _on_disk(path) -> dict:
    return json.loads(path.read_text())


def _loaded_engine(tmp_path, prior=None) -> tuple[RecoveryEngine, object]:
    engine = _engine()
    path = tmp_path / "state.json"
    engine._sm.load_state(path)
    engine._sm.state.current_state = GuardianState.CONFIRMED_DEAD
    engine._sm.state.live_drop_attempt = prior
    return engine, path


class _Cancelled(Box):
    """The tick is cancelled (a SIGTERM) while the engine call runs."""

    async def __call__(self, *args, timeout=10.0, **kwargs):
        if "deploy_candidates drop" in (args[-1] if args else ""):
            raise asyncio.CancelledError
        return await super().__call__(*args, timeout=timeout, **kwargs)


async def test_a_drop_cancelled_mid_call_leaves_the_attempt_on_disk(tmp_path):
    engine, path = _loaded_engine(tmp_path)
    with _patched(_Cancelled()), pytest.raises(asyncio.CancelledError):
        await engine.execute(_drop_diagnosis())
    saved = _on_disk(path)
    assert saved["current_state"] == "recovered" and saved["recovery_attempts"] == 1
    assert saved["live_drop_attempt"]["branch"] == "feat/b"
    assert saved["live_drop_attempt"]["pinned_head"] == PIN_B
    # The next tick (a fresh process) holds through the backoff, then decides.
    sm = ConfirmationStateMachine(engine._config)
    sm.load_state(path)
    dead = MagicMock(all_alive=False, failed_signals=[], pause_state=MagicMock(paused=False))
    assert sm.process(dead).new_state == GuardianState.RECOVERED  # 100 s of backoff
    sm.state.last_recovery_at = "2000-01-01T00:00:00+00:00"
    t = sm.process(dead)
    assert t.new_state == GuardianState.CONFIRMED_DEAD and t.action_needed


async def test_any_action_cancelled_mid_call_leaves_a_counted_recovered_on_disk(tmp_path):
    engine, path = _loaded_engine(tmp_path)

    async def cancelled(*_a):
        raise asyncio.CancelledError

    with (
        _patched(Box()),  # update.lock reads free
        patch.object(engine, "_execute_action", cancelled),
        pytest.raises(asyncio.CancelledError),
    ):
        await engine.execute(_drop_diagnosis(RecoveryAction.RESTART_SERVICES))
    saved = _on_disk(path)
    assert saved["current_state"] == "recovered" and saved["recovery_attempts"] == 1
    assert saved["live_drop_attempt"] is None


_PRIOR = {"branch": "feat/c", "pinned_head": "9" * 40, "tip": "8" * 40, "at": "2026-11-20T09:00:00+00:00"}


@pytest.mark.parametrize("prior", [None, _PRIOR])
@pytest.mark.parametrize(
    "drop", [(1, "", "ERROR: x; nothing changed."), (1, "", "Error: websocket")]
)  # exit 200 is a lock wait (B2), with its own test
async def test_a_clean_refusal_restores_the_prior_attempt_on_disk(tmp_path, prior, drop):
    """The third row is refused by the engine's view (unchanged), not its words."""
    engine, path = _loaded_engine(tmp_path, prior)
    with _patched(Box(drop=drop, views=(BEFORE,))):
        result = await engine.execute(_drop_diagnosis())
    assert not result.success and "nothing" in result.detail
    assert engine._sm.state.live_drop_attempt == prior
    assert _on_disk(path)["live_drop_attempt"] == prior
    # Today's latch rule: a prior attempt still holds RECOVERED, none does not.
    want = GuardianState.RECOVERED if prior else GuardianState.CONFIRMED_DEAD
    assert engine._sm.current_state == want


async def test_the_backoff_runs_from_the_actions_end_counted_once():
    engine = _engine()

    async def slow(*_a):  # the attempt was counted long before the action ended
        engine._sm.state.last_recovery_at = "2000-01-01T00:00:00+00:00"
        return False, "x"

    with _patched(Box()), patch.object(engine, "_execute_action", slow):
        await engine.execute(_drop_diagnosis(RecoveryAction.RESTART_SERVICES))
    assert engine._sm.state.recovery_attempts == 1
    assert engine._sm.recovery_backoff_remaining_s("RESTART_SERVICES") > 95


async def test_a_checkpoint_that_cannot_be_saved_runs_nothing(tmp_path):
    """Round 1: no action starts without its outcome-unknown checkpoint on disk."""
    engine, path = _loaded_engine(tmp_path, _PRIOR)
    st = engine._sm.state
    st.recovery_attempts, st.last_recovery_at = 1, "2000-01-01T00:00:00+00:00"
    st.down_alert_sent = True
    before = {**st.to_dict(), "down_alert_sent": False}  # the next check re-decides
    action = AsyncMock(return_value=(True, "ran"))
    sent = AsyncMock()
    with (
        _patched(Box()),  # update.lock reads free (B2)
        patch.object(engine, "_execute_action", action),
        patch.object(engine._sm, "save_state", MagicMock(return_value=False)),
        patch.object(engine._dispatcher, "send", sent),
    ):
        result = await engine.execute(_drop_diagnosis())
    action.assert_not_awaited()
    assert not result.success and not result.deferred
    assert [a.args[0].severity.value for a in sent.await_args_list] == ["critical"]
    alert = sent.await_args.args[0]
    assert "could not save its state before REVERT_CODE; nothing was run" in alert.title + alert.body
    assert str(path) in alert.body and "next check" in alert.body
    assert engine._sm.state.to_dict() == before


# ── B2: update.lock on `live` (#4841 rework) ─────────────────────────────────

from genesis.guardian import cgroup_ops, live_drop  # noqa: E402


class LockBox(Box):
    """Box plus update.lock: ``lock`` the probe's successive exit codes (the
    last repeats), ``queue`` the exit of a restart queued under the lock (0:
    as Box), ``status`` what `incus info` reports, ``on_live`` the branch."""

    def __init__(self, lock=(0,), queue=0, status="Running", on_live=True, **kw):
        super().__init__(**kw)
        self.lock, self.queue, self.status, self.on_live = list(lock), queue, status, on_live

    async def __call__(self, *args, timeout=10.0, **kwargs):
        cmd = args[-1] if args[1:2] == ("exec",) else " ".join(args)
        if "flock -n -E 75" in cmd:
            self.calls.append(cmd)
            return (self.lock.pop(0) if len(self.lock) > 1 else self.lock[0], "", "")
        if "update_in_progress.pid" in cmd:
            self.calls.append(cmd)
            return (0, "a banner\ninode=77\npid=4242\n", "")
        if cmd.startswith("incus info"):
            self.calls.append(cmd)
            return (0, f"Name: genesis\nStatus: {self.status}\n", "")
        if "flock -n -o -E 75" in cmd and self.queue:
            self.calls.append(cmd)
            return (self.queue, "", "")
        if "symbolic-ref" in cmd and not self.on_live:
            self.calls.append(cmd)
            return (0, "refs/heads/main\n", "")
        if "live_checkout.py" in cmd and not self.on_live:
            self.calls.append(cmd)
            return (1, "other\n", "")
        return await super().__call__(*args, timeout=timeout, **kwargs)

    def sent(self, needle: str) -> list[str]:
        return [c for c in self.calls if needle in c]


@pytest.mark.parametrize(
    "rc,state", [(0, "free"), (75, "held"), (1, "unreadable"), (-1, "unreadable")]
)
async def test_the_lock_probe_maps_its_exit_code(rc, state):
    with _patched(LockBox(lock=(rc,))):
        got, who, inode = await live_drop.lock_state("genesis")
    assert got == state
    if state == "held":
        assert "4242" in who and inode == "77"


@pytest.mark.parametrize(
    "answer,stopped",
    [
        ((0, "Name: genesis\nStatus: STOPPED\n", ""), True),
        ((0, "Name: genesis\nStatus: Frozen\n", ""), False),
        ((0, "Name: genesis\nStatus: Running\n", ""), False),
        ((1, "Status: STOPPED\n", "error"), False),
        ((-1, "", "timeout"), False),
    ],
)
async def test_only_a_reported_stopped_counts(answer, stopped):
    with patch("genesis.guardian.live_drop._run_subprocess", AsyncMock(return_value=answer)):
        assert (await live_drop.container_status("genesis") == "stopped") is stopped


def _sh(cmd: str, home):
    import os
    import subprocess

    env = {**os.environ, "GENESIS_HOME": str(home)}
    return subprocess.run(["bash", "-c", cmd], env=env, capture_output=True, text=True, timeout=20)


def test_the_probe_and_the_queued_restart_under_a_real_flock(tmp_path):
    """Real bash and flock on a scratch lock: free, held, no lock dir; the
    queued command inherits no lock fd and leaves the lock free on exit."""
    import subprocess
    import time

    from genesis.guardian.recovery import _UNDER_LOCK

    home = tmp_path / "gh"
    run = _UNDER_LOCK.format(cmd="sh -c 'ls -l /proc/$$/fd; echo ran'")
    assert _sh(live_drop._LOCK_PROBE, home).returncode == 0  # no lock dir: free
    assert not (home / "locks").exists()  # the probe made none
    r = _sh(run, home)
    assert r.returncode == 0 and "ran" in r.stdout
    (home / "locks").mkdir(parents=True)
    r = _sh(run, home)
    assert r.returncode == 0 and "ran" in r.stdout
    assert "update.lock" not in r.stdout  # -o: no inherited lock fd
    assert _sh(live_drop._LOCK_PROBE, home).returncode == 0  # free after exit
    holder = subprocess.Popen(["flock", str(home / "locks" / "update.lock"), "sleep", "30"])
    try:
        deadline = time.monotonic() + 10
        while _sh(live_drop._LOCK_PROBE, home).returncode != 75:
            assert time.monotonic() < deadline
            time.sleep(0.05)
        r = _sh(run, home)
        assert r.returncode == 75 and "ran" not in r.stdout
    finally:
        holder.kill()
        holder.wait()


_EVERY = [a for a in RecoveryAction if a != RecoveryAction.ESCALATE]


@pytest.mark.parametrize("lock", [75, 1])
@pytest.mark.parametrize("action", _EVERY)
async def test_c3_no_action_runs_while_update_lock_blocks(tmp_path, action, lock):
    """Nothing is counted, snapshotted or saved as RECOVERED; on disk the
    episode waits in CONFIRMED_DEAD, and the one message is the wait alert."""
    engine, path = _loaded_engine(tmp_path)
    ran, sent, take = AsyncMock(), AsyncMock(return_value=True), AsyncMock()
    with (
        _patched(LockBox(lock=(lock,))),
        patch.object(engine, "_execute_action", ran),
        patch.object(engine._dispatcher, "send", sent),
        patch.object(engine._snapshots, "take", take),
    ):
        result = await engine.execute(_drop_diagnosis(action))
    ran.assert_not_awaited()
    take.assert_not_awaited()
    assert result.waiting and not result.success and not result.deferred
    saved = _on_disk(path)
    assert saved["current_state"] == "confirmed_dead" and saved["waiting_on_update_lock"] is True
    assert saved["down_alert_sent"] is True  # the next ticks reach the wait tick
    assert saved["recovery_attempts"] == saved["io_triage_attempts"] == 0
    assert saved["last_recovery_at"] is None and saved["live_drop_attempt"] is None
    # Held: the one wait alert. Unreadable: none now; the next tick's first
    # message is the frozen APPROVE gate (owner ruling 91998f4f).
    assert [c.args[0].title for c in sent.await_args_list] == (
        ["Genesis down — waiting for a deploy to finish"] if lock == 75 else []
    )


async def test_c3_unreadable_with_the_container_stopped_runs_the_action():
    engine = _engine()
    ran = AsyncMock(return_value=(False, "x"))
    with _patched(LockBox(lock=(1,), status="STOPPED")), patch.object(engine, "_execute_action", ran):
        result = await engine.execute(_drop_diagnosis(RecoveryAction.RESTART_CONTAINER))
    ran.assert_awaited_once()
    assert not result.waiting


@pytest.mark.parametrize("armed,on_live", [(False, True), (True, False)])
async def test_c3_flag_off_or_off_live_changes_nothing(armed, on_live):
    engine = _engine(armed=armed)
    box = LockBox(lock=(75,), on_live=on_live)
    ran = AsyncMock(return_value=(False, "x"))
    with _patched(box), patch.object(engine, "_execute_action", ran):
        result = await engine.execute(_drop_diagnosis(RecoveryAction.RESTART_SERVICES))
    ran.assert_awaited_once()
    assert not result.waiting and not engine._sm.state.waiting_on_update_lock
    assert bool(box.sent("flock")) is armed  # off: the lock is never read


@pytest.mark.parametrize("lock,runs", [(1, True), (75, False)])
async def test_the_frozen_approval_runs_on_unreadable_and_refuses_held(lock, runs):
    engine = _engine()
    ran = AsyncMock(return_value=(True, "Container restarted"))
    probes = AsyncMock(return_value=MagicMock(all_alive=True))
    with (
        _patched(LockBox(lock=(lock,))),
        patch.object(engine, "_execute_action", ran),
        patch("genesis.guardian.recovery.collect_all_signals", probes),
    ):
        result = await engine.execute(
            _drop_diagnosis(RecoveryAction.RESTART_CONTAINER), owner_approved_unreadable=True
        )
    assert ran.await_count == int(runs) and result.waiting is not runs


async def test_a_lock_taken_during_the_snapshot_defers_and_restores_the_counters(tmp_path):
    engine, path = _loaded_engine(tmp_path)
    engine._sm.state.recovery_attempts = 1
    engine._sm.state.last_recovery_at = before = "2026-01-01T00:00:00+00:00"
    ran = AsyncMock()
    with (
        _patched(LockBox(lock=(0, 75))),
        patch.object(engine, "_execute_action", ran),
        patch.object(engine._snapshots, "safe_to_snapshot", AsyncMock(return_value=True)),
        patch.object(engine._snapshots, "take", AsyncMock(return_value="pre-recovery-1")),
    ):
        result = await engine.execute(_drop_diagnosis(RecoveryAction.RESTART_CONTAINER))
    ran.assert_not_awaited()
    saved = _on_disk(path)
    assert result.waiting and saved["recovery_attempts"] == 1
    assert saved["last_recovery_at"] == before and saved["current_state"] == "confirmed_dead"


# ── the restart queued under update.lock ─────────────────────────────────────

_RESTART = "systemctl --user restart --no-block genesis-server"


async def test_flag_off_the_restart_command_is_unchanged():
    box = LockBox()
    with _patched(box):
        await _engine(armed=False)._restart_services("genesis")
    assert box.sent("--no-block") == [_RESTART] and not box.sent("flock")


async def test_armed_the_restart_is_queued_under_update_lock():
    from genesis.guardian.recovery import _UNDER_LOCK

    box = LockBox()
    with _patched(box):
        ok, _ = await _engine()._restart_services("genesis")
    assert ok and box.sent("--no-block") == [_UNDER_LOCK.format(cmd=_RESTART)]


async def test_a_held_queue_off_live_restarts_as_before():
    box = LockBox(queue=75, on_live=False)
    with _patched(box):
        ok, _ = await _engine()._restart_services("genesis")
    assert ok and box.sent("--no-block")[-1] == _RESTART


async def test_a_held_queue_on_live_defers_with_no_attempt_and_no_failed_alert(tmp_path):
    engine, path = _loaded_engine(tmp_path)
    sent = AsyncMock(return_value=True)
    with _patched(LockBox(queue=75)), patch.object(engine._dispatcher, "send", sent):
        result = await engine.execute(_drop_diagnosis(RecoveryAction.RESTART_SERVICES))
    saved = _on_disk(path)
    assert result.waiting and saved["recovery_attempts"] == 0 and saved["last_recovery_at"] is None
    assert saved["current_state"] == "confirmed_dead" and saved["waiting_on_update_lock"] is True
    assert saved["down_alert_sent"] is True  # over the checkpoint's cleared latch (#5017)
    titles = [c.args[0].title for c in sent.await_args_list]
    assert titles[0] == "Attempting recovery: RESTART_SERVICES" and len(titles) == 2
    assert not any("Recovery failed" in t for t in titles)


@pytest.mark.parametrize("prior", [None, _PRIOR])
@pytest.mark.parametrize(
    "drop",
    [(200, "", ""), (1, "", "ERROR: a live deploy (pid 5) holds the deploy marker /x. Nothing changed.")],
)
async def test_drop_exit_200_and_the_deploy_marker_are_waits_that_cost_nothing(tmp_path, prior, drop):
    engine, path = _loaded_engine(tmp_path, prior)
    with _patched(LockBox(drop=drop, views=(BEFORE,))):
        result = await engine.execute(_drop_diagnosis())
    saved = _on_disk(path)
    assert result.waiting and saved["recovery_attempts"] == 0
    assert saved["live_drop_attempt"] == prior
    assert saved["current_state"] == "confirmed_dead"  # never RECOVERED: nothing ran


@pytest.mark.parametrize("action", [RecoveryAction.REVERT_CODE, RecoveryAction.RESOURCE_CLEAR])
async def test_a_restart_held_after_work_ran_keeps_the_attempt(tmp_path, action):
    """A drop that changed `live` (exit 4: dropped, restart not queued), or
    RESOURCE_CLEAR's cleanup, ran before the restart was held."""
    engine, path = _loaded_engine(tmp_path)
    sent = AsyncMock(return_value=True)
    with _patched(LockBox(drop=(4, "", ""), queue=75)), patch.object(engine._dispatcher, "send", sent):
        result = await engine.execute(_drop_diagnosis(action))
    saved = _on_disk(path)
    body = sent.await_args.args[0].body  # SF1: the wait alert says what already ran
    assert "Nothing was restarted or dropped" not in body
    assert f"The approved {action.value} step ran; only the genesis-server restart is held" in body
    assert result.waiting and saved["recovery_attempts"] == 1
    assert saved["current_state"] == "confirmed_dead" and saved["waiting_on_update_lock"] is True
    if action == RecoveryAction.REVERT_CODE:
        assert saved["live_drop_attempt"]["branch"] == "feat/b"


# ── the host-side hint (measured: subtree pids, /proc/locks, cmdline) ────────


def _host(tmp_path, monkeypatch):
    cg, proc = tmp_path / "cg", tmp_path / "proc"
    (cg / "init.scope").mkdir(parents=True)
    (cg / "cgroup.procs").write_text("")  # the top level lists nothing (measured)
    (cg / "init.scope" / "cgroup.procs").write_text("100\n200\n")
    proc.mkdir()
    (proc / "locks").write_text(
        "1: FLOCK  ADVISORY  WRITE 200 00:2f:77 0 EOF\n"
        "2: FLOCK  ADVISORY  WRITE 999 00:2f:77 0 EOF\n"  # not in the container
        "3: -> FLOCK  ADVISORY  WRITE 100 00:2f:77 0 EOF\n"  # a waiter
        "4: FLOCK  ADVISORY  WRITE 100 00:2f:78 0 EOF\n"  # another file
    )
    for pid, argv in ((200, b"bash\0scripts/update.sh\0"), (100, b"python3\0-m\0genesis\0")):
        (proc / str(pid)).mkdir()
        (proc / str(pid) / "cmdline").write_bytes(argv)
    monkeypatch.setattr(cgroup_ops, "_cgroup_path", lambda _c: cg)
    monkeypatch.setattr(cgroup_ops, "_PROC", proc)


def test_the_host_names_the_update_lock_holder_by_inode(tmp_path, monkeypatch):
    _host(tmp_path, monkeypatch)
    assert cgroup_ops.lock_holders("genesis", "77") == [(200, "bash scripts/update.sh")]
    assert cgroup_ops.lock_holders("genesis", "5") == []


def test_without_the_inode_the_host_names_deploy_processes(tmp_path, monkeypatch):
    """A frozen guest cannot say update.lock's inode: cmdlines are the hint."""
    _host(tmp_path, monkeypatch)
    assert cgroup_ops.lock_holders("genesis") == [(200, "bash scripts/update.sh")]


async def test_the_host_hint_is_text_and_bounded(tmp_path, monkeypatch):
    import time

    _host(tmp_path, monkeypatch)
    assert "pid 200 (bash scripts/update.sh)" in await live_drop.host_hint("genesis", "77")
    assert "none seen" in await live_drop.host_hint("genesis", "5")
    monkeypatch.setattr(cgroup_ops, "lock_holders", lambda *_a: time.sleep(1))
    monkeypatch.setattr(live_drop, "_HINT_TIMEOUT_S", 0.05)
    assert await live_drop.host_hint("genesis", "77") == ""

    def broken(*_a):
        raise OSError("no")

    monkeypatch.setattr(cgroup_ops, "lock_holders", broken)
    assert await live_drop.host_hint("genesis", "77") == ""


async def test_the_restored_state_is_on_disk_before_the_wait_alert(tmp_path):
    """NOTE 4: a tick killed while the alert is sent leaves the wait, not
    #5017's checkpoint (an attempt counted that never ran, the latch clear)."""
    engine, path = _loaded_engine(tmp_path)
    seen: list[dict] = []

    async def send(alert):
        seen.append(_on_disk(path))
        return True

    with _patched(LockBox(queue=75)), patch.object(engine._dispatcher, "send", send):
        await engine.execute(_drop_diagnosis(RecoveryAction.RESTART_SERVICES))
    at_wait_alert = seen[-1]
    assert at_wait_alert["recovery_attempts"] == 0 and at_wait_alert["waiting_on_update_lock"]
    assert at_wait_alert["down_alert_sent"] is True
