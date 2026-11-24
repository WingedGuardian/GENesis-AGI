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


def _boot(boot: str, live: str = NEW_TIP, state: str = "active") -> str:
    return (
        f"a login banner\nActiveState={state}\nSubState=running\nInvocationID=x\nResult=success\n"
        f"live={live}\nboot={boot}"
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
        ((200, "", ""), GuardianState.CONFIRMED_DEAD),  # a clean refusal arms nothing
    ],
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
