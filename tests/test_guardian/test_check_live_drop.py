"""The Guardian on a `live` checkout, before the drop runs (#2978).

``recovery.live_engine`` gates everything here: off (the default) the Guardian
behaves on `live` exactly as before (REVERT_CODE becomes RESTART_CONTAINER, no
candidate is read); on, a REVERT_CODE proposal becomes the drop of the newest
candidate the engine names, decided before Gate 2. Also here: the flag's
coercion, the persisted attempt record, and the evidence the diagnosis gets.
The container is a fake that answers each command by what it contains."""

from __future__ import annotations

import html
import json
import logging
import re
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.guardian import live_drop
from genesis.guardian.alert.base import AlertSeverity
from genesis.guardian.alert.telegram import _utf16_units
from genesis.guardian.check import _check_cycle, _execute_recovery_with_approval
from genesis.guardian.collector import DiagnosticSnapshot, collect_diagnostics
from genesis.guardian.config import GuardianConfig, RecoveryConfig, load_config
from genesis.guardian.diagnosis import (
    DiagnosisEngine,
    DiagnosisResult,
    RecoveryAction,
    _build_diagnosis_prompt,
)
from genesis.guardian.health_signals import HealthSnapshot, PauseState, SignalResult
from genesis.guardian.recovery import RecoveryEngine
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
    preview="auto",
) -> dict:
    if preview == "auto":  # the engine's plan for the newest: nothing else leaves
        held = [b for b, _ in holds or () if b in dict(listed)]
        preview = {"branch": held[-1], "leaving": []} if held else None
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
        "drop_preview": preview,
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
        f"live={live}\nboot={boot}\nafter=9\n"
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
    assert got.drop_candidate == ("feat/b", PIN_B, HEAD_B, TIP, ())
    assert "drops the newest candidate" in got.likely_cause
    assert "Drops feat/b" in got.gate2_identity and "No other candidate" in got.gate2_identity


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
    assert all(c in got.gate2_identity for c in (PIN_B, HEAD_B, TIP))


async def test_an_attempt_still_pinned_and_newest_reruns_the_same_drop():
    got = await _propose(Container(_view()), _attempt())
    assert got.drop_candidate == ("feat/b", PIN_B, HEAD_B, TIP, ())
    assert "re-runs the drop attempted at 2026-11-20T10:00:00" in got.gate2_identity


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
    assert got.drop_candidate == ("feat/a", PIN_A, HEAD_A, NEW_TIP, ())
    assert "feat/b" in got.gate2_identity  # the earlier drop is named


@pytest.mark.parametrize("state", ["failed", "activating", "inactive"])
async def test_a_crash_loop_on_the_new_tip_still_reaches_the_next_candidate(state):
    """The server is not running, but its last boot was from `live`'s tip: the
    drop took and the next candidate is the suspect, not a missing restart."""
    box = Container(AFTER, boot=_boot(NEW_TIP, NEW_TIP, state=state))
    got = await _propose(box, _attempt())
    assert got.drop_candidate == ("feat/a", PIN_A, HEAD_A, NEW_TIP, ())


async def test_a_half_done_view_after_an_attempt_says_the_drop_may_still_be_running():
    """Between the manifest save and the move, a running drop looks half-done."""
    mid = _view(listed=(("feat/a", PIN_A),))
    got = await _propose(Container(mid), _attempt())
    assert got.recommended_action == RecoveryAction.ESCALATE and got.drop_candidate is None
    assert "may still be running" in got.likely_cause and "update.lock" in got.likely_cause


async def test_gate_2_names_every_candidate_the_drops_own_plan_takes_out():
    view = _view(preview={"branch": "feat/b", "leaving": ["feat/a"]})
    got = await _propose(Container(view))
    assert got.drop_candidate == ("feat/b", PIN_B, HEAD_B, TIP, ("feat/a",))
    assert "Also leaving `live` with it: feat/a" in got.gate2_identity


#: Round-2 premise check, class B rule 4: no drop unless every candidate it
#: removes can be listed in full.
_HUGE = [f"feat/{'n' * 240}-{i:02d}" for i in range(15)]  # 15 x 249 characters


@pytest.mark.parametrize(
    "preview,needle",
    [
        (None, "could not plan which candidates leave"),
        ({"branch": "feat/a", "leaving": []}, "planned the drop of feat/a, not of feat/b"),
        ({"branch": "feat/b", "leaving": ["bad name"]}, "could not plan which candidates leave"),
        ({"branch": "feat/b", "leaving": _HUGE}, "the 15 candidates leaving `live` with feat/b"),
    ],
)
async def test_no_drop_is_proposed_unless_gate_2_can_list_all_it_removes(preview, needle):
    got = await _propose(Container(_view(preview=preview)))
    assert got.recommended_action == RecoveryAction.RESTART_CONTAINER and got.drop_candidate is None
    assert needle in got.likely_cause and not got.gate2_identity


@pytest.mark.parametrize("state", ["error", "foreign"])
async def test_an_unread_manifest_is_never_read_as_an_empty_one(state):
    """observe() lists nothing when the manifest cannot be read: held candidates
    must not read as dropped, nor the attempted one as gone."""
    view = _view(listed=())
    view["manifest"] = {"state": state, "reason": "x"}
    got = await _propose(Container(view, boot=_boot(TIP, TIP)), _attempt())
    assert got.recommended_action == RecoveryAction.RESTART_CONTAINER
    assert got.drop_candidate is None and "could not be read" in got.likely_cause


async def test_an_unsettled_checkout_after_an_attempt_proposes_nothing():
    """A drop that timed out may still be moving the tree: neither the pre-drop
    tip nor a settled `live`. Nothing runs (B2's lock checkpoint owns waiting),
    and the text promises no later look that never comes (SHOULD-FIX 5)."""
    mid = _view(checkout=("refs/heads/live", "d" * 40))
    got = await _propose(Container(mid), _attempt())
    assert got.recommended_action == RecoveryAction.ESCALATE and got.drop_candidate is None
    assert "may still be running" in got.likely_cause
    assert "looks again" not in got.likely_cause
    assert "asks again only if this episode auto-resets" in got.likely_cause


async def test_an_attempted_branch_repinned_at_a_new_head_proposes_the_current_newest():
    view = _view(listed=(("feat/a", PIN_A), ("feat/b", "5" * 40)))
    got = await _propose(Container(view), _attempt())
    assert got.drop_candidate == ("feat/b", "5" * 40, HEAD_B, TIP, ())
    assert "re-pinned" in got.gate2_identity


async def test_an_attempted_branch_no_longer_newest_proposes_the_current_newest():
    view = _view(
        listed=(("feat/b", PIN_B), ("feat/a", PIN_A)),
        holds=(("feat/b", HEAD_B), ("feat/a", HEAD_A)),
    )
    got = await _propose(Container(view), _attempt())
    assert got.drop_candidate == ("feat/a", PIN_A, HEAD_A, TIP, ())
    assert "no longer the newest" in got.gate2_identity


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


async def _run_gates(config, box, sm=None, diagnosis=None, engine=None, signals=None):
    channel = _gate_channel()
    if engine is None:
        engine = MagicMock()
        engine.execute = AsyncMock()
    else:
        engine.execute = AsyncMock(wraps=engine.execute)
    collect = AsyncMock(return_value={})
    with (
        patch("genesis.guardian.check._find_telegram_channel", return_value=channel),
        patch(
            "genesis.guardian.check.collect_all_signals",
            signals or AsyncMock(return_value=_snapshot(alive=False)),
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
    assert executed.drop_candidate == ("feat/b", PIN_B, HEAD_B, TIP, ())
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
    assert "re-runs the drop attempted at" in executed.gate2_identity
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


# ── Gate 2: what the approval removes, never cut ─────────────────────────────


def _gate2(texts: list[str]) -> str:
    return next(t for t in texts if "Proposed:" in t)


def _parsed_units(text: str) -> int:
    """What Telegram counts: the parsed text, tags gone and entities one character."""
    return _utf16_units(html.unescape(re.sub(r"<[^>]+>", "", text)))


_MANY = [f"feat/{'n' * 30}-{i:02d}" for i in range(40)]


async def test_gate_2_keeps_every_identifier_with_a_long_diagnosis_and_many_leaving():
    """Round-2 premise check, class B rules 2 and 3: the identity section is never
    cut; the diagnosis takes what remains of 4,096 units and says it was cut."""
    view = _view(preview={"branch": "feat/b", "leaving": _MANY})
    long = replace(_diagnosis(), likely_cause="x" * 9000)
    executed, texts, _ = await _run_gates(_armed(), Container(view), diagnosis=long)
    assert executed.drop_candidate == ("feat/b", PIN_B, HEAD_B, TIP, tuple(_MANY))
    gate2 = _gate2(texts)
    for ident in [*_MANY, PIN_B, HEAD_B, TIP, "scripts/deploy_candidates add feat/b --owner"]:
        assert ident in gate2, ident
    assert "(cut)" in gate2 and _parsed_units(gate2) <= 4096


async def test_a_short_gate_2_is_not_cut():
    _, texts, _ = await _run_gates(_armed(), Container(_view()))
    gate2 = _gate2(texts)
    assert "(cut)" not in gate2 and "server crash loop after a deploy" in gate2


# ── SHOULD-FIX 6: a container-wide action while a drop may run ───────────────


@pytest.mark.parametrize(
    "action,view",
    [
        (RecoveryAction.RESTART_CONTAINER, _view(checkout=("refs/heads/live", "d" * 40))),
        (RecoveryAction.SNAPSHOT_ROLLBACK, None),
        (RecoveryAction.REVERT_CODE, None),  # an unreadable view falls back to the restart
    ],
)
async def test_a_container_action_while_a_drop_may_run_says_it_would_interrupt_it(action, view):
    config = _armed()
    sm = _dead_sm(config)
    sm.state.live_drop_attempt = _attempt()
    executed, texts, _ = await _run_gates(
        config, Container(view), sm=sm, diagnosis=_diagnosis(action)
    )
    assert executed.recommended_action != RecoveryAction.REVERT_CODE
    gate2 = _gate2(texts)
    assert "a drop attempted at 2026-11-20T10:00:00+00:00 may still be running" in gate2
    assert "this action would interrupt it" in gate2


async def test_a_container_restart_after_a_settled_drop_has_no_warning():
    config = _armed()
    sm = _dead_sm(config)
    sm.state.live_drop_attempt = _attempt()
    _, texts, _ = await _run_gates(
        config, Container(_view()), sm=sm, diagnosis=_diagnosis(RecoveryAction.RESTART_CONTAINER)
    )
    assert "would interrupt it" not in _gate2(texts)


# ── S3 / SHOULD-FIX 4: an approved action that is deferred says so ───────────


async def test_s3_an_approved_action_that_is_deferred_says_so():
    """Both gates approved, and the action does not run: the owner is told. Still
    held after the in-tick wait (#5007; here no time passes), it says so again."""
    config = _armed()
    sm = _dead_sm(config)
    sm.record_recovery_attempt("RESTART_SERVICES")  # 100 s of backoff from now
    engine = RecoveryEngine(config, sm, MagicMock(), MagicMock(send=AsyncMock()))
    with patch("asyncio.sleep", new_callable=AsyncMock):
        _, texts, _ = await _run_gates(
            config,
            Container(_view(), on_live=False),
            sm=sm,
            diagnosis=_diagnosis(RecoveryAction.RESTART_SERVICES),
            engine=engine,
        )
    assert any(t.startswith("Approved RESTART_SERVICES did not run yet") for t in texts), texts
    assert any(
        t.startswith("Approved RESTART_SERVICES did not run: backoff") and "s remaining" in t
        for t in texts
    ), texts


# ── class A: the drop's outcome reaches the next decision ────────────────────


def _ago(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


class Episode:
    """Ticks 30 s apart through the real state machine, _check_cycle and
    RecoveryEngine.execute, the state round-tripped through JSON each tick (each
    tick is a fresh oneshot process). Both gates approve at once; the drop is a
    fake that records an attempt (unless ``records`` is false) and fails."""

    def __init__(self, resets_left: bool = False, records: bool = True):
        self.config = _armed()
        self.sm = ConfirmationStateMachine(self.config)
        st = self.sm.state
        st.current_state = GuardianState.CONFIRMED_DEAD
        st.first_failure_at = _ago(3600)
        st.auto_reset_count = 0 if resets_left else self.config.confirmation.max_auto_resets
        self.dispatcher = MagicMock(send=AsyncMock())
        self.snaps = MagicMock(safe_to_snapshot=AsyncMock(return_value=False))
        self.engine = RecoveryEngine(self.config, self.sm, self.snaps, self.dispatcher)
        self.records = records
        self.approved: list[str] = []
        self.ran = 0
        self.approve = self._approve
        self.outcome: str | None = None

    async def _approve(self, config, sm, dispatcher, engine, diagnosis):
        self.approved.append(str(diagnosis.recommended_action))
        await engine.execute(diagnosis)

    async def _action(self, action, diagnosis=None):
        self.ran += 1
        if not self.records:  # a clean refusal: the record before the call is back
            return self.engine._refused("the drop of feat/b was refused, nothing changed")
        self.sm.state.live_drop_attempt = _attempt()
        return False, "the drop of feat/b: outcome was not seen"

    def alerts(self, severity=None) -> list:
        sent = [c.args[0] for c in self.dispatcher.send.await_args_list]
        return [a for a in sent if severity is None or a.severity == severity]

    async def tick(self, n: int = 1) -> None:
        for _ in range(n):
            st = self.sm.state
            for key in ("first_failure_at", "last_recovery_at"):
                try:  # a deliberately unreadable stamp stays as it is
                    when = datetime.fromisoformat(getattr(st, key) or "")
                except ValueError:
                    continue
                setattr(st, key, (when - timedelta(seconds=30)).isoformat())
            self.sm._state = StateData.from_dict(json.loads(json.dumps(st.to_dict())))
            found = replace(_diagnosis(), drop_candidate=("feat/b", PIN_B, HEAD_B, TIP, ()))
            if self.outcome:
                found = replace(found, outcome=self.outcome)
            with (
                patch(
                    "genesis.guardian.check.collect_all_signals",
                    AsyncMock(return_value=_snapshot(alive=False)),
                ),
                patch(
                    "genesis.guardian.check.collect_diagnostics",
                    AsyncMock(return_value=DiagnosticSnapshot(collected_at="t")),
                ),
                patch("genesis.guardian.check.write_diagnosis_result"),
                patch("genesis.guardian.check._execute_recovery_with_approval", self.approve),
                patch("genesis.guardian.check._handle_healthy", AsyncMock()),
                patch("genesis.guardian.check._handle_confirming", AsyncMock()),
                patch("genesis.guardian.check._handle_surveying", AsyncMock()),
                patch.object(self.engine, "_execute_action", self._action),
            ):
                await _check_cycle(
                    self.config,
                    self.sm,
                    self.dispatcher,
                    self.snaps,
                    MagicMock(diagnose=AsyncMock(return_value=found)),
                    self.engine,
                )


async def test_s2_a_failed_drop_with_resets_exhausted_reaches_every_next_decision():
    """Measured silent for 2 h at round 2: the latch held after the first drop."""
    ep = Episode()
    await ep.tick(240)
    assert ep.approved == ["REVERT_CODE"] * 3 + ["ESCALATE"] and ep.ran == 3
    assert len(ep.alerts(AlertSeverity.EMERGENCY)) == 1


async def test_s4_a_failed_drop_waits_out_its_backoff_then_is_diagnosed_not_auto_reset():
    """An auto-reset while the outcome is pending would route the episode away
    from the next decision; RECOVERED holds it through the backoff instead."""
    ep = Episode(resets_left=True)
    ep.sm.state.first_failure_at = _ago(550)  # the 600 s auto-reset passes during the backoff
    await ep.tick(1)
    assert ep.ran == 1 and ep.sm.current_state == GuardianState.RECOVERED
    await ep.tick(5)  # 100 s of backoff, then the one transition
    assert ep.sm.state.auto_reset_count == 0
    assert ep.approved == ["REVERT_CODE", "REVERT_CODE"]


@pytest.mark.parametrize("outcome", [None, "resolved"])
async def test_s5_an_exception_after_an_action_alerts_once_and_keeps_the_latch(outcome):
    """Both approval routes: a diagnosis that claims "resolved" still goes
    through the gates (_handle_cc_resolved), so it raises under the same alert."""
    ep = Episode()
    await ep.tick(1)
    ep.outcome = outcome

    async def broken(*_a):
        raise RuntimeError("the approval flow broke")

    ep.approve = broken
    with pytest.raises(RuntimeError):
        await ep.tick(5)
    assert ep.sm.state.down_alert_sent is True
    said = [a for a in ep.alerts(AlertSeverity.CRITICAL) if "after a recovery action" in a.title]
    await ep.tick(10)  # never a prompt or an alert every tick
    assert len(said) == 1
    assert len([a for a in ep.alerts() if "after a recovery action" in a.title]) == 1


async def test_s6_at_max_escalations_the_drop_outcome_reaches_the_escalation():
    """Backoff is infinite there: holding for it would never end, so the one
    transition goes straight to the EMERGENCY escalation."""
    ep = Episode()
    st = ep.sm.state
    st.current_state = GuardianState.RECOVERED
    st.recovery_attempts = ep.config.recovery.max_escalations
    st.last_recovery_at = _ago(0)
    st.live_drop_attempt = _attempt()
    st.down_alert_sent = True
    await ep.tick(1)
    assert ep.approved == ["ESCALATE"] and len(ep.alerts(AlertSeverity.EMERGENCY)) == 1


@pytest.mark.parametrize(
    "stamp", [None, "not a time", "2026-01-01T00:00:00", "2999-01-01T00:00:00+00:00"]
)
async def test_an_unusable_recovery_stamp_restarts_the_backoff_and_the_drop_runs(stamp):
    """A missing, unparsable, naive or future stamp froze the backoff at its full
    length: RECOVERED would hold forever, or the next approved drop was deferred
    forever with the latch spent. It now counts down once, from the tick that
    found it."""
    ep = Episode()
    st = ep.sm.state
    st.current_state = GuardianState.RECOVERED
    st.recovery_attempts = 1
    st.last_recovery_at = stamp
    st.live_drop_attempt = _attempt()
    st.down_alert_sent = True
    await ep.tick(1)
    assert ep.sm.current_state == GuardianState.RECOVERED and not ep.approved
    await ep.tick(5)  # the 100 s backoff, counted from the first tick
    assert ep.approved == ["REVERT_CODE"] and ep.ran == 1


async def test_a_failure_with_no_drop_attempt_keeps_the_latch_as_before():
    """Control: a clean refusal (rc 200) records nothing and arms nothing (B2
    owns the lock wait)."""
    ep = Episode(records=False)
    await ep.tick(60)
    assert ep.approved == ["REVERT_CODE"] and ep.sm.current_state == GuardianState.CONFIRMED_DEAD


# ── #5007 item 2: a deferred approved action waits out its own backoff ───────


def _io_deferred(config: GuardianConfig) -> tuple[ConfirmationStateMachine, RecoveryEngine, list, list]:
    """io=4 of 5, main=1: IO_TRIAGE's own 240 s backoff has 130 s left."""
    sm = _dead_sm(config)
    sm.state.recovery_attempts = 1
    sm.state.io_triage_attempts = 4
    sm.state.last_recovery_at = _ago(110)
    snaps = MagicMock(safe_to_snapshot=AsyncMock(return_value=False))
    engine = RecoveryEngine(config, sm, snaps, MagicMock(send=AsyncMock()))
    ran: list = []
    slept: list = []

    async def action(a, d=None):
        ran.append(a)
        return False, "no I/O consumer found"

    async def sleep(s):  # the wait passes: the stamp moves back by it
        slept.append(s)
        when = datetime.fromisoformat(sm.state.last_recovery_at) - timedelta(seconds=s)
        sm.state.last_recovery_at = when.isoformat()

    engine._execute_action = action
    return sm, engine, ran, (slept, sleep)


async def test_a_deferred_io_triage_waits_out_its_own_backoff_then_runs_once():
    config = GuardianConfig()  # every install, not only `live`
    sm, engine, ran, (slept, sleep) = _io_deferred(config)
    with patch("asyncio.sleep", sleep):
        _, texts, _ = await _run_gates(
            config, Container(_view(), on_live=False), sm=sm,
            diagnosis=_diagnosis(RecoveryAction.IO_TRIAGE), engine=engine,
        )
    assert ran == [RecoveryAction.IO_TRIAGE] and sm.state.io_triage_attempts == 5
    assert len(slept) == 1 and 125 < slept[0] < 140
    assert any(t.startswith("Approved IO_TRIAGE did not run yet") and "waits" in t for t in texts)
    assert not any("did not run:" in t for t in texts), texts


async def test_a_deferred_action_does_not_run_when_genesis_is_back_after_the_wait():
    config = GuardianConfig()
    sm, engine, ran, (slept, sleep) = _io_deferred(config)

    async def signals(_config):
        return _snapshot(alive=bool(slept))

    with patch("asyncio.sleep", sleep):
        _, texts, _ = await _run_gates(
            config, Container(_view(), on_live=False), sm=sm,
            diagnosis=_diagnosis(RecoveryAction.IO_TRIAGE), engine=engine, signals=signals,
        )
    assert ran == [] and slept and sm.state.down_alert_sent is False
    assert any("recovered on its own" in t for t in texts), texts


async def test_an_exhausted_io_budget_escalates_before_gate_1():
    """The owner is never asked to approve an action that cannot run."""
    from genesis.guardian.check import _handle_confirmed_dead

    config = GuardianConfig()
    sm = _dead_sm(config)
    sm.state.down_alert_sent = False
    sm.state.io_triage_attempts = config.recovery.max_io_triage_attempts
    approve = AsyncMock()
    with (
        patch("genesis.guardian.check.collect_diagnostics", AsyncMock(return_value={})),
        patch("genesis.guardian.check.write_diagnosis_result"),
        patch("genesis.guardian.check._execute_recovery_with_approval", approve),
    ):
        await _handle_confirmed_dead(
            config, sm, MagicMock(send=AsyncMock()),
            MagicMock(diagnose=AsyncMock(return_value=_diagnosis(RecoveryAction.IO_TRIAGE))),
            MagicMock(),
        )
    assert approve.await_args.args[4].recommended_action == RecoveryAction.ESCALATE


async def test_an_exhausted_io_budget_proposed_after_gate_1_escalates_before_gate_2():
    config = GuardianConfig()
    sm = _dead_sm(config)
    sm.state.io_triage_attempts = config.recovery.max_io_triage_attempts
    executed, texts, _ = await _run_gates(
        config, Container(_view(), on_live=False), sm=sm,
        diagnosis=_diagnosis(RecoveryAction.IO_TRIAGE),
    )
    assert executed.recommended_action == RecoveryAction.ESCALATE
    assert not any("Proposed:" in t for t in texts)


# ── #5007 item 1: a SIGTERM'd tick still saves its state ─────────────────────


_CHILD = r"""
import asyncio, sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
sys.path.insert(0, sys.argv[2])
from genesis.guardian import check
from genesis.guardian.config import GuardianConfig
from genesis.guardian.state_machine import GuardianState

state_dir = Path(sys.argv[1])
config = GuardianConfig()
config.state_dir = str(state_dir)
config.maintenance_file = str(state_dir / "no-maintenance")

async def cycle(config, sm, *_a):
    # A gate wait with no reply: the episode's latch is set, then it blocks.
    sm.set_confirmed_dead()
    sm.mark_down_alert_sent()
    (state_dir / "ready").write_text("1")
    await asyncio.Event().wait()

with (
    patch.object(check, "_check_cycle", cycle),
    patch.object(check, "_build_dispatcher", MagicMock()),
    patch.object(check, "SnapshotManager", MagicMock()),
    patch.object(check, "DiagnosisEngine", MagicMock()),
    patch.object(check, "_drain_host_alert_queue", AsyncMock()),
    patch.object(check, "_check_guard_layer_and_alert", AsyncMock()),
    patch.object(check, "_check_pool_relief", AsyncMock()),
    patch.object(check, "_gateway_pause_active", MagicMock(return_value=False)),
    patch.object(check, "read_boot_id", MagicMock(return_value=None)),
):
    asyncio.run(check.run_check(config))
"""


def _sigtermed_tick(tmp_path):
    import signal
    import subprocess
    import sys
    import time
    from pathlib import Path

    src = str(Path(__file__).resolve().parents[2] / "src")
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(tmp_path), src],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        deadline = time.monotonic() + 60
        while not (tmp_path / "ready").exists():
            assert child.poll() is None, child.communicate()
            assert time.monotonic() < deadline, "the child never reached the cycle"
            time.sleep(0.05)
        child.send_signal(signal.SIGTERM)
        child.wait(timeout=30)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
    return child


def test_a_sigtermed_tick_still_writes_its_state_file(tmp_path):
    child = _sigtermed_tick(tmp_path)
    state = tmp_path / "state.json"
    assert child.returncode != 0 and state.exists(), child.stderr.read()
    assert json.loads(state.read_text())["current_state"] == "confirmed_dead"


def test_a_gate_wait_killed_at_the_timeout_keeps_its_latch_until_the_auto_reset(tmp_path):
    """Behaviour change, named for the owner (S7): the latch set before the wait
    is saved, so the episode stays quiet; the 600 s auto-reset still re-opens it."""
    _sigtermed_tick(tmp_path)
    config = GuardianConfig()
    sm = ConfirmationStateMachine(config)
    sm.load_state(tmp_path / "state.json")
    assert sm.state.down_alert_sent is True
    assert sm.process(_snapshot(alive=False)).new_state == GuardianState.CONFIRMED_DEAD
    sm.state.first_failure_at = _ago(config.confirmation.confirmed_dead_timeout_s + 1)
    t = sm.process(_snapshot(alive=False))
    assert t.new_state == GuardianState.HEALTHY and "auto-reset" in t.reason
    assert sm.state.down_alert_sent is True  # unchanged: only a genuine recovery clears it
