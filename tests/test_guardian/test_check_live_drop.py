"""The Guardian on a `live` checkout, before the drop runs (#2978).

``recovery.live_engine`` gates everything here: off (the default) the Guardian
behaves on `live` exactly as before (REVERT_CODE becomes RESTART_CONTAINER, no
candidate is read); on, a REVERT_CODE proposal becomes the drop of the newest
candidate the engine names, decided before Gate 2. Also here: the flag's
coercion, the persisted attempt record, and the evidence the diagnosis gets.
The container is a fake that answers each command by what it contains."""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.guardian import live_drop
from genesis.guardian.alert.telegram import _utf16_units
from genesis.guardian.check import _execute_recovery_with_approval, _gate2_cause
from genesis.guardian.collector import DiagnosticSnapshot, collect_diagnostics
from genesis.guardian.config import GuardianConfig, RecoveryConfig, load_config
from genesis.guardian.diagnosis import (
    DiagnosisEngine,
    DiagnosisResult,
    RecoveryAction,
    _build_diagnosis_prompt,
)
from genesis.guardian.health_signals import HealthSnapshot, PauseState, SignalResult
from genesis.guardian.state_machine import ConfirmationStateMachine, GuardianState, StateData

TIP = "a" * 40
NEW_TIP = "c" * 40
HEAD_A = "1" * 40
HEAD_B = "2" * 40
PIN_A = "3" * 40
PIN_B = "4" * 40


def _view(
    listed=(("feat/a", PIN_A), ("feat/b", PIN_B)),
    holds=(("feat/a", HEAD_A), ("feat/b", HEAD_B)),
    tip=TIP,
    refusals=(),
    checkout=("refs/heads/live", TIP),
) -> dict:
    return {
        "version": 1,
        "manifest": {"state": "ok", "reason": None},
        "base": "b" * 40,
        "listed": [{"branch": b, "head": h, "in_checkout": True} for b, h in listed],
        "live": {
            "tip": tip,
            "holds": None if holds is None else [{"branch": b, "head": h} for b, h in holds],
            "reason": None,
        },
        "checkout": None if checkout is None else {"branch_ref": checkout[0], "head": checkout[1]},
        "move_refusals": list(refusals),
    }


#: `live` after feat/b's drop: feat/a alone, at a new tip.
AFTER = _view(
    listed=(("feat/a", PIN_A),),
    holds=(("feat/a", HEAD_A),),
    tip=NEW_TIP,
    checkout=("refs/heads/live", NEW_TIP),
)


def _boot(boot: str, live: str, state: str = "active") -> str:
    return (
        f"a login banner\nActiveState={state}\nSubState=running\nInvocationID=9\nResult=success\n"
        f"live={live}\nboot={boot}\n"
    )


class Container:
    """Answers the Guardian's commands: the engine's JSON view, its text list,
    the server-boot probe and the two live probes. Records every command."""

    def __init__(self, view=None, text="", boot=None, on_live=True):
        self.view, self.text, self.boot, self.on_live = view, text, boot, on_live
        self.calls: list[str] = []

    async def __call__(self, *args, timeout=10.0, **kwargs):
        cmd = args[-1] if args[1:2] == ("exec",) else " ".join(args)
        self.calls.append(cmd)
        if "list --json --move-refusals" in cmd:
            if self.view is None:
                return (1, "", "boom")
            return (0, "a login banner\n" + json.dumps(self.view), "")
        if "deploy_candidates list" in cmd:
            return (0, self.text, "")
        if "serving_commit.py" in cmd:
            return (0, self.boot or _boot(TIP, TIP), "")
        if "symbolic-ref" in cmd:
            return (0, "refs/heads/live\n" if self.on_live else "refs/heads/main\n", "")
        if "live_checkout.py" in cmd:
            return (0, "live\n", "") if self.on_live else (1, "other\n", "")
        return (0, "", "")

    def sent(self, needle: str) -> list[str]:
        return [c for c in self.calls if needle in c]


def _diagnosis(action=RecoveryAction.REVERT_CODE) -> DiagnosisResult:
    return DiagnosisResult(
        likely_cause="server crash loop after a deploy",
        confidence_pct=80,
        evidence=[],
        recommended_action=action,
        reasoning="proposed for approval",
        source="cc",
        outcome="proposed",
    )


def _armed(config: GuardianConfig | None = None) -> GuardianConfig:
    config = config or GuardianConfig()
    config.recovery.live_engine = True
    return config


async def _propose(box, attempt=None) -> DiagnosisResult:
    with patch("genesis.guardian.live_drop._run_subprocess", box):
        return await live_drop.live_revert_proposal("genesis", _diagnosis(), attempt)


def _attempt(branch="feat/b", pinned=PIN_B, tip=TIP) -> dict:
    return {"branch": branch, "pinned_head": pinned, "tip": tip, "at": "2026-11-20T10:00:00+00:00"}


# ── the flag ─────────────────────────────────────────────────────────────────


def test_live_engine_defaults_off():
    assert GuardianConfig().recovery.live_engine is False


@pytest.mark.parametrize("raw", ["true", "false", 1, "yes", None])
def test_a_non_bool_live_engine_reads_off_and_warns(raw, caplog):
    """`_build_sub` passes raw YAML, so a quoted "false" would be truthy: only
    a real bool arms the engine, and anything else says so in the log."""
    with caplog.at_level(logging.WARNING):
        assert RecoveryConfig(live_engine=raw).live_engine is False
    assert "live_engine" in caplog.text


def test_live_engine_true_in_yaml_arms(tmp_path):
    path = tmp_path / "guardian.yaml"
    path.write_text("recovery:\n  live_engine: true\n")
    assert load_config(path).recovery.live_engine is True


# ── the attempt record ───────────────────────────────────────────────────────


def test_the_attempt_record_round_trips():
    state = StateData(live_drop_attempt=_attempt())
    assert (
        StateData.from_dict(json.loads(json.dumps(state.to_dict()))).live_drop_attempt == _attempt()
    )


@pytest.mark.parametrize(
    "raw",
    [
        "feat/b",
        ["feat/b"],
        {"branch": "feat/b"},
        {**_attempt(), "tip": 5},
        {**_attempt(), "at": None},
    ],
)
def test_a_corrupt_attempt_record_reads_as_none(raw):
    assert StateData.from_dict({"live_drop_attempt": raw}).live_drop_attempt is None


def _snapshot(alive: bool) -> HealthSnapshot:
    sig = SignalResult("health_api", alive, 1.0, "ok" if alive else "down", "t")
    return HealthSnapshot(signals={"health_api": sig}, pause_state=PauseState(paused=False))


def test_the_record_outlives_auto_reset_deny_and_a_failed_send():
    """Tied to the episode: the 600 s auto-reset (`_reset_to_healthy`) and
    `clear_down_alert_sent` (DENY, a failed Gate-2 send) both keep it."""
    config = GuardianConfig()
    sm = ConfirmationStateMachine(config)
    sm.state.live_drop_attempt = _attempt()
    sm.state.current_state = GuardianState.CONFIRMED_DEAD
    sm.state.first_failure_at = "2000-01-01T00:00:00+00:00"  # long past the timeout
    sm.clear_down_alert_sent()
    t = sm.process(_snapshot(alive=False))
    assert t.new_state == GuardianState.HEALTHY and "auto-reset" in t.reason
    assert sm.state.live_drop_attempt == _attempt()


def test_the_record_ends_with_the_episode():
    sm = ConfirmationStateMachine(GuardianConfig())
    sm.state.live_drop_attempt = _attempt()
    sm.state.current_state = GuardianState.RECOVERED
    sm.process(_snapshot(alive=True))
    assert sm.state.live_drop_attempt is None


# ── the proposal (armed) ─────────────────────────────────────────────────────


async def test_the_newest_candidate_is_the_last_held_that_is_listed():
    got = await _propose(Container(_view()))
    assert got.recommended_action == RecoveryAction.REVERT_CODE
    assert got.drop_candidate == ("feat/b", PIN_B, HEAD_B, TIP)
    assert "drop the newest candidate feat/b" in got.likely_cause


async def test_a_half_done_drop_escalates_to_rebuild():
    got = await _propose(Container(_view(listed=(("feat/a", PIN_A),))))
    assert got.recommended_action == RecoveryAction.ESCALATE
    assert got.escalation == live_drop.HALF_DONE_DROP and got.drop_candidate is None
    assert "deploy_candidates rebuild" in got.likely_cause


async def test_a_drop_the_engine_would_refuse_is_not_proposed():
    got = await _propose(Container(_view(refusals=("the checkout is dirty",))))
    assert got.recommended_action == RecoveryAction.RESTART_CONTAINER
    assert got.drop_candidate is None and "the checkout is dirty" in got.likely_cause


async def test_an_unreadable_list_falls_back_to_the_container_restart():
    got = await _propose(Container(None))
    assert got.recommended_action == RecoveryAction.RESTART_CONTAINER
    assert "could not be read" in got.likely_cause


async def test_a_listed_row_without_a_head_is_not_a_view():
    view = _view()
    del view["listed"][1]["head"]
    got = await _propose(Container(view))
    assert got.recommended_action == RecoveryAction.RESTART_CONTAINER


async def test_differing_pinned_and_held_heads_are_both_named():
    got = await _propose(Container(_view()))
    assert PIN_B[:12] in got.likely_cause and HEAD_B[:12] in got.likely_cause


async def test_an_attempt_still_pinned_and_newest_reruns_the_same_drop():
    got = await _propose(Container(_view()), _attempt())
    assert got.drop_candidate == ("feat/b", PIN_B, HEAD_B, TIP)
    assert "re-runs the drop attempted at 2026-11-20T10:00:00" in got.likely_cause


async def test_a_finished_drop_the_server_has_not_booted_onto_proposes_a_restart():
    box = Container(AFTER, boot=_boot(TIP, NEW_TIP))  # still booted from the pre-drop tip
    got = await _propose(box, _attempt())
    assert got.recommended_action == RecoveryAction.RESTART_SERVICES and got.drop_candidate is None
    assert "the drop of feat/b finished" in got.likely_cause
    assert "may not have restarted onto it" in got.likely_cause


async def test_a_finished_drop_with_an_unknown_boot_proposes_a_restart():
    box = Container(AFTER, boot=_boot("unknown: the reflog is empty", NEW_TIP))
    got = await _propose(box, _attempt())
    assert got.recommended_action == RecoveryAction.RESTART_SERVICES


async def test_a_finished_drop_the_server_booted_onto_proposes_the_next_candidate():
    """Owner ruling: a still-sick server after a drop that took may get the
    next drop, approved at its own Gate 2."""
    box = Container(AFTER, boot=_boot(NEW_TIP, NEW_TIP))
    got = await _propose(box, _attempt())
    assert got.drop_candidate == ("feat/a", PIN_A, HEAD_A, NEW_TIP)
    assert "feat/b" in got.likely_cause  # the earlier drop is named


@pytest.mark.parametrize("state", ["failed", "activating", "inactive"])
async def test_a_crash_loop_on_the_new_tip_still_reaches_the_next_candidate(state):
    """The server is not running, but its last boot was from `live`'s tip: the
    drop took and the next candidate is the suspect, not a missing restart."""
    box = Container(AFTER, boot=_boot(NEW_TIP, NEW_TIP, state=state))
    got = await _propose(box, _attempt())
    assert got.drop_candidate == ("feat/a", PIN_A, HEAD_A, NEW_TIP)


async def test_a_half_done_view_after_an_attempt_says_the_drop_may_still_be_running():
    """Between the manifest save and the move, a running drop looks half-done."""
    mid = _view(listed=(("feat/a", PIN_A),))
    got = await _propose(Container(mid), _attempt())
    assert got.recommended_action == RecoveryAction.ESCALATE and got.drop_candidate is None
    assert "may still be running" in got.likely_cause and "update.lock" in got.likely_cause


async def test_an_unreadable_view_after_an_attempt_names_the_drop_in_the_restart():
    got = await _propose(Container(None), _attempt())
    assert got.recommended_action == RecoveryAction.RESTART_CONTAINER
    assert "feat/b" in got.likely_cause and "may still be running" in got.likely_cause


def test_the_gate_2_cause_stays_deliverable_and_keeps_what_runs():
    """Telegram refuses a message over 4,096 units, and a refused Gate 2 runs no
    recovery. The drop clause is at the END of the cause, so the end is kept."""
    cause = "x" * 9000 + " — On `live`, REVERT_CODE means: drop the newest candidate feat/b"
    got = _gate2_cause(cause)
    assert _utf16_units(got) < 3100 and got.endswith("candidate feat/b")
    assert _gate2_cause("short") == "short"


async def test_an_unsettled_checkout_after_an_attempt_proposes_nothing():
    """A drop that timed out may still be moving the tree: neither the pre-drop
    tip nor a settled `live`. Nothing runs (B2's lock checkpoint owns waiting)."""
    mid = _view(checkout=("refs/heads/live", "d" * 40))
    got = await _propose(Container(mid), _attempt())
    assert got.recommended_action == RecoveryAction.ESCALATE and got.drop_candidate is None
    assert "may still be running" in got.likely_cause


async def test_an_attempted_branch_repinned_at_a_new_head_proposes_the_current_newest():
    view = _view(listed=(("feat/a", PIN_A), ("feat/b", "5" * 40)))
    got = await _propose(Container(view), _attempt())
    assert got.drop_candidate == ("feat/b", "5" * 40, HEAD_B, TIP)
    assert "re-pinned" in got.likely_cause


async def test_an_attempted_branch_no_longer_newest_proposes_the_current_newest():
    view = _view(
        listed=(("feat/b", PIN_B), ("feat/a", PIN_A)),
        holds=(("feat/b", HEAD_B), ("feat/a", HEAD_A)),
    )
    got = await _propose(Container(view), _attempt())
    assert got.drop_candidate == ("feat/a", PIN_A, HEAD_A, TIP)
    assert "no longer the newest" in got.likely_cause


# ── check.py wiring ──────────────────────────────────────────────────────────


def _gate_channel() -> MagicMock:
    channel = MagicMock()
    channel.send_text = AsyncMock(return_value=1)
    channel.poll_for_keyword = AsyncMock(side_effect=["APPROVE", "APPROVE"])
    return channel


def _dead_sm(config: GuardianConfig) -> ConfirmationStateMachine:
    sm = ConfirmationStateMachine(config)
    sm.state.current_state = GuardianState.CONFIRMED_DEAD
    sm.state.down_alert_sent = True
    sm.state.first_failure_at = datetime.now(UTC).isoformat()
    return sm


async def _run_gates(config, box, sm=None, diagnosis=None):
    channel = _gate_channel()
    engine = MagicMock()
    engine.execute = AsyncMock()
    collect = AsyncMock(return_value={})
    with (
        patch("genesis.guardian.check._find_telegram_channel", return_value=channel),
        patch(
            "genesis.guardian.check.collect_all_signals",
            AsyncMock(return_value=_snapshot(alive=False)),
        ),
        patch("genesis.guardian.check.collect_diagnostics", collect),
        patch("genesis.guardian.check.write_diagnosis_result"),
        patch.object(
            DiagnosisEngine, "diagnose", AsyncMock(return_value=diagnosis or _diagnosis())
        ),
        patch(
            "genesis.guardian.git_watch.container_git_supports_revert",
            AsyncMock(return_value=True),
        ),
        patch("genesis.guardian.recovery._run_subprocess", box),
        patch("genesis.guardian.live_drop._run_subprocess", box),
    ):
        await _execute_recovery_with_approval(
            config, sm or _dead_sm(config), MagicMock(send=AsyncMock()), engine, _diagnosis()
        )
    texts = [c.args[0] for c in channel.send_text.await_args_list]
    return engine.execute.await_args.args[0], texts, collect


async def test_flag_off_on_live_is_main_restart_container_and_reads_no_candidates():
    """Inert: with the default config the proposal is main's RESTART_CONTAINER,
    with main's text, and the engine's list is never read."""
    box = Container(_view())
    executed, texts, _ = await _run_gates(GuardianConfig(), box)
    assert executed.recommended_action == RecoveryAction.RESTART_CONTAINER
    assert "it ends lock holders and lets a boot repair" in executed.likely_cause
    assert not box.sent("deploy_candidates") and not box.sent("serving_commit")


async def test_armed_on_live_the_owner_approves_the_named_drop():
    executed, texts, _ = await _run_gates(_armed(), Container(_view()))
    assert executed.recommended_action == RecoveryAction.REVERT_CODE
    assert executed.drop_candidate == ("feat/b", PIN_B, HEAD_B, TIP)
    gate2 = next(t for t in texts if "Proposed:" in t)
    assert "feat/b" in gate2


async def test_armed_off_live_is_a_plain_revert():
    box = Container(_view(), on_live=False)
    executed, _, _ = await _run_gates(_armed(), box)
    assert executed.recommended_action == RecoveryAction.REVERT_CODE
    assert executed.drop_candidate is None and not box.sent("deploy_candidates")


async def test_armed_the_attempt_record_reaches_the_proposal():
    config = _armed()
    sm = _dead_sm(config)
    sm.state.live_drop_attempt = _attempt()
    executed, _, collect = await _run_gates(config, Container(_view()), sm=sm)
    assert "re-runs the drop attempted at" in executed.likely_cause
    assert collect.await_args.args[1] == _attempt()


async def test_flag_off_the_attempt_record_is_not_read():
    config = GuardianConfig()
    sm = _dead_sm(config)
    sm.state.live_drop_attempt = _attempt()
    executed, _, collect = await _run_gates(config, Container(_view()), sm=sm)
    assert executed.recommended_action == RecoveryAction.RESTART_CONTAINER
    assert collect.await_args.args[1] is None


async def test_a_half_done_drop_tells_the_owner_to_rebuild():
    box = Container(_view(listed=(("feat/a", PIN_A),)))
    executed, texts, _ = await _run_gates(_armed(), box)
    assert executed.escalation == live_drop.HALF_DONE_DROP
    assert any("finishes the drop" in t for t in texts)


# ── the diagnosis evidence ───────────────────────────────────────────────────


_TEXT = "Deploy manifest:\n  1. feat/a  added 2026-11-01  head 333333333333\n  2. feat/b  added 2026-11-02\n"


async def test_flag_off_the_collector_reads_no_candidates():
    box = Container(_view(), text=_TEXT)
    with (
        patch("genesis.guardian.live_drop._run_subprocess", box),
        patch("genesis.guardian._subprocess.run_subprocess", AsyncMock(return_value=(1, "", ""))),
    ):
        snap = await collect_diagnostics(GuardianConfig(), _attempt())
    assert snap.live_candidates == "" and not box.calls


async def test_armed_the_collector_lists_candidates_and_names_the_attempt():
    box = Container(_view(), text=_TEXT)
    with (
        patch("genesis.guardian.live_drop._run_subprocess", box),
        patch("genesis.guardian._subprocess.run_subprocess", AsyncMock(return_value=(1, "", ""))),
    ):
        snap = await collect_diagnostics(_armed(), _attempt())
    assert "2. feat/b" in snap.live_candidates
    assert "feat/b" in snap.live_candidates.splitlines()[-1]
    assert "2026-11-20T10:00:00" in snap.live_candidates
    assert "LIVE CANDIDATES" in snap.to_prompt_text()


def test_without_candidates_the_prompt_is_unchanged():
    assert "LIVE CANDIDATES" not in DiagnosticSnapshot(collected_at="t").to_prompt_text()


def test_the_inventory_names_the_drop_only_when_armed():
    snap = DiagnosticSnapshot(collected_at="t")
    off = _build_diagnosis_prompt(snap, "", "genesis")
    on = _build_diagnosis_prompt(snap, "", "genesis", live_engine=True)
    assert "never on `live`, where a bad candidate is removed by" in off
    assert "drops the newest candidate" not in off
    assert "drops the newest candidate" in on


def test_a_drop_candidate_survives_replace():
    d = replace(_diagnosis(), drop_candidate=("feat/b", PIN_B, HEAD_B, TIP))
    assert d.drop_candidate[1] == PIN_B and d.escalation == ""
