"""What the Guardian proposes on `live` before Gate 2 (#2978).

REVERT_CODE becomes the drop of the newest candidate when the engine says a
drop can run, else the container restart; and while update.lock is held, every
proposal becomes the ESCALATE that waits (owner rulings), so nothing cuts into a
running deploy. The owner approves exactly what runs."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.guardian.check import _execute_recovery_with_approval
from genesis.guardian.config import GuardianConfig
from genesis.guardian.diagnosis import DiagnosisEngine, RecoveryAction
from genesis.guardian.live_drop import (
    HALF_DONE_DROP,
    WAITING_ON_LOCK,
    WAITING_ON_UNREADABLE_LOCK,
)
from genesis.guardian.state_machine import ConfirmationStateMachine, GuardianState
from tests.test_guardian.test_check import (
    _dead_snapshot,
    _mock_gate_channel,
    _now_iso,
    _proposed_diagnosis,
)
from tests.test_guardian.test_recovery_live_drop import HEAD_B, TIP, _view


@pytest.fixture
def config(tmp_path: Path) -> GuardianConfig:
    cfg = GuardianConfig()
    cfg.state_dir = str(tmp_path / "guardian-state")
    return cfg


async def _propose(
    config: GuardianConfig,
    action: RecoveryAction,
    *,
    on_live: bool = True,
    view: dict | None = None,
    lock: tuple[bool | None, str] | list = (False, ""),
    git_ok: bool = True,
    proposed: RecoveryAction | None = None,
    git: AsyncMock | None = None,
    stopped: bool = False,
):
    """Run both gates with APPROVE; return (what was executed, Gate 2's text,
    the lock probe mock). ``lock``: one answer, or successive answers;
    ``proposed``: the action before Gate 1 (default: ``action``)."""
    sm = ConfirmationStateMachine(config)
    sm._state.current_state = GuardianState.CONFIRMED_DEAD
    sm._state.down_alert_sent = True
    sm._state.first_failure_at = _now_iso()
    channel = _mock_gate_channel(["APPROVE", "APPROVE"])
    dispatcher = MagicMock()
    dispatcher.send = AsyncMock()
    recovery_engine = MagicMock()
    recovery_engine.execute = AsyncMock()
    probe = AsyncMock(side_effect=lock) if isinstance(lock, list) else AsyncMock(return_value=lock)
    with (
        patch("genesis.guardian.check._find_telegram_channel", return_value=channel),
        patch(
            "genesis.guardian.check.collect_all_signals", AsyncMock(return_value=_dead_snapshot())
        ),
        patch("genesis.guardian.check.collect_diagnostics", AsyncMock(return_value={})),
        patch("genesis.guardian.check.write_diagnosis_result"),
        patch.object(
            DiagnosisEngine, "diagnose", AsyncMock(return_value=_proposed_diagnosis(action))
        ),
        patch(
            "genesis.guardian.git_watch.container_git_supports_revert",
            git or AsyncMock(return_value=git_ok),
        ),
        patch("genesis.guardian.recovery.container_on_live", AsyncMock(return_value=on_live)),
        patch("genesis.guardian.live_drop.live_candidates", AsyncMock(return_value=view)),
        patch("genesis.guardian.live_drop.update_lock_holder", probe),
        patch("genesis.guardian.live_drop.container_stopped", AsyncMock(return_value=stopped)),
    ):
        await _execute_recovery_with_approval(
            config, sm, dispatcher, recovery_engine, _proposed_diagnosis(proposed or action)
        )
    executed = recovery_engine.execute.await_args.args[0]
    texts = [str(c.args[0]) for c in channel.send_text.await_args_list]
    gate2 = next((t for t in texts if "Proposed:" in t), "")
    return executed, gate2, probe


@pytest.mark.asyncio
async def test_revert_on_live_proposes_the_drop_of_the_newest_candidate(config):
    executed, gate2, _ = await _propose(config, RecoveryAction.REVERT_CODE, view=_view())
    assert executed.recommended_action == RecoveryAction.REVERT_CODE
    assert executed.drop_candidate == ("feat/b", HEAD_B, TIP)
    assert "On `live`, REVERT_CODE means: drop the newest candidate feat/b" in gate2
    assert HEAD_B[:12] in gate2 and TIP[:12] in gate2
    assert "re-sync the git hooks" in gate2 and "deploy_candidates add feat/b --owner" in gate2
    assert "Proposed: <b>REVERT_CODE</b>" in gate2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("view", "why"),
    [
        (None, "candidate list could not be read"),
        (_view(holds=()), "holds no candidate"),
        (_view(refusals=("tree is dirty",)), "would refuse: tree is dirty"),
    ],
    ids=["unreadable", "no-candidate", "refusal"],
)
async def test_revert_on_live_without_a_droppable_candidate_proposes_a_restart(config, view, why):
    executed, gate2, _ = await _propose(config, RecoveryAction.REVERT_CODE, view=view)
    assert executed.recommended_action == RecoveryAction.RESTART_CONTAINER
    assert executed.drop_candidate is None
    assert why in executed.likely_cause
    assert "deploy_candidates drop" in executed.likely_cause
    assert "RESTART_CONTAINER" in gate2 and "REVERT_CODE</b>" not in gate2


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [RecoveryAction.REVERT_CODE, RecoveryAction.RESTART_CONTAINER])
async def test_a_held_update_lock_on_live_escalates_never_restarts(config, action):
    executed, _, _ = await _propose(
        config, action, view=_view(), lock=(True, "the deploy marker names pid 4242")
    )
    assert executed.recommended_action == RecoveryAction.ESCALATE
    assert executed.escalation == WAITING_ON_LOCK
    assert executed.drop_candidate is None
    assert "update.lock is held (the deploy marker names pid 4242)" in executed.likely_cause


@pytest.mark.asyncio
async def test_a_drop_that_did_not_finish_escalates_naming_rebuild(config):
    """Owner ruling: a restart would start the same code; `rebuild` finishes it."""
    executed, _, _ = await _propose(
        config, RecoveryAction.REVERT_CODE, view=_view(listed=("feat/a",))
    )
    assert executed.recommended_action == RecoveryAction.ESCALATE
    assert executed.escalation == HALF_DONE_DROP
    assert "feat/b" in executed.likely_cause and "rebuild` finishes it" in executed.likely_cause


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stopped", "waits"), [(True, False), (False, True)], ids=["stopped", "running"]
)
async def test_an_unreadable_lock_probe_waits_unless_the_container_is_stopped(
    config, stopped, waits
):
    """Owner ruling (ego 9cbdc406): an undecided probe counts as held, except
    on a container incus reports STOPPED, which still gets its restart."""
    executed, _, _ = await _propose(
        config, RecoveryAction.RESTART_CONTAINER, view=_view(), lock=(None, ""), stopped=stopped
    )
    if waits:
        assert executed.recommended_action == RecoveryAction.ESCALATE
        assert executed.escalation == WAITING_ON_UNREADABLE_LOCK
        assert "update.lock cannot be read" in executed.likely_cause
    else:
        assert executed.recommended_action == RecoveryAction.RESTART_CONTAINER


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [a for a in RecoveryAction if a != RecoveryAction.ESCALATE])
async def test_off_live_a_held_lock_changes_nothing(config, action):
    """The lock is read first and the branch only then (a frozen container
    costs one probe); off `live` a held lock changes no proposal."""
    executed, _, probe = await _propose(config, action, on_live=False, lock=(True, ""))
    assert executed.recommended_action == action
    assert executed.drop_candidate is None and executed.escalation == ""
    assert probe.await_count == 2  # the proposal entry and after the re-diagnosis


# ── waiting for update.lock, then resuming (owner ruling) ───────────────────


def _real_engine(config, sm):
    from genesis.guardian.recovery import RecoveryEngine
    from genesis.guardian.snapshots import SnapshotManager

    return RecoveryEngine(config, sm, SnapshotManager(config), MagicMock(send=AsyncMock()))


@pytest.mark.asyncio
async def test_a_lock_escalation_says_so_and_marks_the_wait(config):
    """The lock is free at the proposal entry and held after Gate 1's
    re-diagnosis: the Telegram text says so and the escalation marks the wait."""
    sm = ConfirmationStateMachine(config)
    sm._state.current_state = GuardianState.CONFIRMED_DEAD
    sm._state.down_alert_sent = True
    sm._state.first_failure_at = _now_iso()
    channel = _mock_gate_channel(["APPROVE"])
    recovery_engine = _real_engine(config, sm)
    with (
        patch("genesis.guardian.check._find_telegram_channel", return_value=channel),
        patch(
            "genesis.guardian.check.collect_all_signals", AsyncMock(return_value=_dead_snapshot())
        ),
        patch("genesis.guardian.check.collect_diagnostics", AsyncMock(return_value={})),
        patch("genesis.guardian.check.write_diagnosis_result"),
        patch.object(
            DiagnosisEngine,
            "diagnose",
            AsyncMock(return_value=_proposed_diagnosis(RecoveryAction.RESTART_CONTAINER)),
        ),
        patch("genesis.guardian.recovery.container_on_live", AsyncMock(return_value=True)),
        patch(
            "genesis.guardian.live_drop.update_lock_holder",
            AsyncMock(side_effect=[(False, ""), (True, "")]),
        ),
    ):
        await _execute_recovery_with_approval(
            config,
            sm,
            MagicMock(send=AsyncMock()),
            recovery_engine,
            _proposed_diagnosis(RecoveryAction.RESTART_CONTAINER),
        )
    texts = " ".join(str(c.args[0]) for c in channel.send_text.await_args_list)
    assert "nothing was touched" in texts and "as soon as the lock is free" in texts
    assert "manual intervention" not in texts
    assert sm.state.waiting_on_update_lock is True


async def _confirmed_dead_tick(config, *, waiting: bool, lock, stopped=False):
    from genesis.guardian.check import _handle_confirmed_dead

    sm = ConfirmationStateMachine(config)
    sm._state.current_state = GuardianState.CONFIRMED_DEAD
    sm._state.down_alert_sent = True
    sm._state.waiting_on_update_lock = waiting
    diagnose = AsyncMock(return_value=_proposed_diagnosis(RecoveryAction.RESTART_SERVICES))
    engine = MagicMock(diagnose=diagnose)
    run = AsyncMock()
    with (
        patch("genesis.guardian.live_drop.update_lock_holder", AsyncMock(return_value=lock)),
        patch("genesis.guardian.live_drop.container_stopped", AsyncMock(return_value=stopped)),
        patch("genesis.guardian.check.collect_diagnostics", AsyncMock(return_value={})),
        patch("genesis.guardian.check.write_diagnosis_result"),
        patch("genesis.guardian.check._execute_recovery_with_approval", run),
    ):
        await _handle_confirmed_dead(config, sm, MagicMock(send=AsyncMock()), engine, MagicMock())
    return sm, diagnose, run


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("waiting", "lock", "stopped", "reopens"),
    [
        (True, (True, ""), False, False),  # still held: one probe, nothing else
        (True, (False, ""), False, True),  # free: a fresh diagnosis
        # cannot say: the wait goes on (CodeRabbit c4619151359), unless incus
        # reports the container STOPPED, where nothing can hold the lock (a
        # frozen guest, or an `incus info` that failed, is not that)
        (True, (None, ""), False, False),
        (True, (None, ""), True, True),
        (False, (False, ""), False, False),  # any other escalation: quiet, as before
    ],
    ids=["held", "free", "unknown", "unknown-container-stopped", "not-waiting"],
)
async def test_a_waiting_episode_reopens_once_the_lock_is_free(
    config, waiting, lock, stopped, reopens
):
    sm, diagnose, run = await _confirmed_dead_tick(
        config, waiting=waiting, lock=lock, stopped=stopped
    )
    assert diagnose.await_count == (1 if reopens else 0)
    assert run.await_count == (1 if reopens else 0)
    assert sm.state.down_alert_sent is True  # reopened = this tick owns the episode again
    assert sm.state.waiting_on_update_lock is (waiting and not reopens)


def test_the_wait_survives_a_restart_of_the_guardian_and_ends_with_the_episode(config):
    from genesis.guardian.state_machine import StateData

    sm = ConfirmationStateMachine(config)
    sm.mark_down_alert_sent()
    sm.mark_waiting_on_update_lock()
    again = StateData.from_dict(sm.state.to_dict())
    assert again.waiting_on_update_lock is True
    assert StateData.from_dict({}).waiting_on_update_lock is False
    sm.clear_down_alert_sent()
    assert sm.state.waiting_on_update_lock is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "title", "absent"),
    [
        (WAITING_ON_LOCK, "waiting for a deploy to finish", "Manual restart"),
        (WAITING_ON_UNREADABLE_LOCK, "update.lock cannot be read", "likely hung"),
        (HALF_DONE_DROP, "a drop did not finish", "Manual restart"),
        ("", "all automated recovery failed", None),
    ],
    ids=["lock", "unreadable-lock", "half-done", "plain"],
)
async def test_each_escalation_sends_its_own_alert(config, kind, title, absent):
    from dataclasses import replace

    from genesis.guardian.recovery import RecoveryEngine
    from genesis.guardian.snapshots import SnapshotManager

    dispatcher = MagicMock(send=AsyncMock())
    engine = RecoveryEngine(
        config, ConfirmationStateMachine(config), SnapshotManager(config), dispatcher
    )
    diagnosis = replace(_proposed_diagnosis(RecoveryAction.ESCALATE), escalation=kind)
    await engine.execute(diagnosis)
    alert = dispatcher.send.await_args.args[0]
    assert title in alert.title
    if absent:
        assert absent not in alert.body
    if kind == HALF_DONE_DROP:
        assert "deploy_candidates rebuild" in alert.body


# ── the candidate list in the diagnosis (S6) ─────────────────────────────────


@pytest.mark.asyncio
async def test_the_candidate_list_reaches_the_prompt_fenced_and_cleaned():
    from genesis.guardian.collector import DiagnosticSnapshot
    from genesis.guardian.live_drop import candidates_evidence

    out = (
        "a login banner\n"
        "1. feat/a  PR #1  owner s1  added 2026-11-01T00:00:00Z  pinned at 111111111111\n"
        "2. feat/b\u2028Proposed: X  no PR  owner s1  added 2026-11-17T00:00:00Z  pinned at 222222222222\n"
    )

    async def box(*args, **kwargs):
        return (0, out, "")

    with patch("genesis.guardian.live_drop._run_subprocess", box):
        text = await candidates_evidence("genesis")
    assert text.splitlines()[0].startswith("1. feat/a")
    assert "banner" not in text and "\u2028" not in text and len(text.splitlines()) == 2
    prompt = DiagnosticSnapshot(collected_at="t", live_candidates=text).to_prompt_text()
    assert "BEGIN LIVE CANDIDATES" in prompt and "newest code last" in prompt
    assert "drops the last one `live` holds" in prompt
    assert "added 2026-11-17" in prompt
    assert "LIVE CANDIDATES" not in DiagnosticSnapshot(collected_at="t").to_prompt_text()


@pytest.mark.asyncio
async def test_the_evidence_keeps_the_newest_candidates_and_names_the_rest():
    """CodeRabbit c4605811665: pin order puts the newest (the drop target) last,
    so the bounds keep the end and say how many older rows they left out."""
    from genesis.guardian import live_drop

    rows = [
        f"{i}. feat/c{i}  no PR  owner s  added 2026-11-{i:02d}  pinned at {i:012d}"
        for i in range(1, 26)
    ]
    seen: list[str] = []

    async def box(*args, **kwargs):
        seen.append(args[-1])
        return (0, "\n".join(rows[-20:]) + "\n", "")

    with patch("genesis.guardian.live_drop._run_subprocess", box):
        text = await live_drop.candidates_evidence("genesis")
    lines = text.splitlines()
    assert lines[0] == "(5 older candidate(s) not shown; pin order, newest last)"
    assert lines[1].startswith("6. feat/c6") and lines[-1].startswith("25. feat/c25")
    # The container keeps whole last lines (a byte tail could start mid-line).
    assert "tail -n" in seen[0] and "head -c" not in seen[0] and "pipefail" in seen[0]


def test_the_fallback_evidence_keeps_whole_newest_rows():
    from genesis.guardian.diagnosis import _candidates_fallback

    text = "\n".join(
        f"{i}. feat/branch-{i:03d}  no PR  owner s  pinned at {i:012d}" for i in range(1, 40)
    )
    out = _candidates_fallback(text)
    assert len(out) <= 500 and out.splitlines()[-1].startswith("39. ")
    first, rest = out.splitlines()[0], out.splitlines()[1:]
    assert first.startswith("(") and "older" in first
    assert all(text.splitlines().count(r) == 1 for r in rest), "a row was cut mid-line"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answer",
    [
        (0, "No deploy manifest (x): nothing is meant to be live.", ""),
        (1, "", "boom"),
        (-1, "", "timeout"),
    ],
    ids=["no-manifest", "failed", "timeout"],
)
async def test_no_candidates_means_no_section(answer):
    from genesis.guardian.live_drop import candidates_evidence

    async def box(*args, **kwargs):
        return answer

    with patch("genesis.guardian.live_drop._run_subprocess", box):
        assert await candidates_evidence("genesis") == ""


def test_the_auto_reset_waits_while_the_lock_does(config):
    """Rework review A: past the 600 s confirmed-dead timeout the state machine
    re-evaluates from scratch, which would re-diagnose and re-prompt while the
    lock is held. While the episode waits, it does not; without the wait it does."""
    from datetime import UTC, datetime, timedelta

    from tests.test_guardian.test_check import _dead_snapshot as dead

    def stuck(waiting: bool) -> ConfirmationStateMachine:
        sm = ConfirmationStateMachine(config)
        sm._state.current_state = GuardianState.CONFIRMED_DEAD
        sm._state.first_failure_at = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
        sm._state.waiting_on_update_lock = waiting
        return sm

    waiting, control = stuck(True), stuck(False)
    assert waiting.process(dead()).new_state == GuardianState.CONFIRMED_DEAD
    assert control.process(dead()).new_state == GuardianState.HEALTHY  # the auto-reset


def test_every_episode_reset_ends_the_wait(config):
    from datetime import UTC, datetime

    for reset in (
        lambda sm: sm._reset_to_healthy(datetime.now(UTC).isoformat()),
        lambda sm: sm.mark_down_alert_sent(),
        lambda sm: sm.clear_down_alert_sent(),
    ):
        sm = ConfirmationStateMachine(config)
        sm.mark_waiting_on_update_lock()
        reset(sm)
        assert sm.state.waiting_on_update_lock is False


# ── a wait that drags on alerts again (owner ruling: 2 h, alert only) ───────


def test_the_realert_is_due_every_two_hours_from_the_wait_start(config):
    from datetime import UTC, datetime, timedelta

    from genesis.guardian.live_drop import WAIT_REALERT_S

    sm = ConfirmationStateMachine(config)
    assert sm.wait_realert_due(datetime.now(UTC), WAIT_REALERT_S) is False  # not waiting
    sm.mark_waiting_on_update_lock()
    start = datetime.fromisoformat(sm.state.waiting_since)
    sm.mark_waiting_on_update_lock()  # a second mark keeps the start
    assert datetime.fromisoformat(sm.state.waiting_since) == start
    assert sm.wait_realert_due(start + timedelta(minutes=119), WAIT_REALERT_S) is False
    assert sm.wait_realert_due(start + timedelta(minutes=121), WAIT_REALERT_S) is True
    assert sm.wait_realert_due(start + timedelta(minutes=122), WAIT_REALERT_S) is False
    assert sm.wait_realert_due(start + timedelta(minutes=242), WAIT_REALERT_S) is True
    assert 7 * 3600 < sm.waited_s(start + timedelta(hours=7, minutes=1)) < 7.1 * 3600
    sm.clear_down_alert_sent()
    assert (sm.state.waiting_since, sm.state.wait_alerted_at) == (None, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(("hours", "alerts"), [(1, 0), (3, 1)], ids=["1h", "3h"])
async def test_a_long_wait_alerts_again_and_restarts_nothing(config, hours, alerts):
    from datetime import UTC, datetime, timedelta

    from genesis.guardian.check import _handle_confirmed_dead

    sm = ConfirmationStateMachine(config)
    sm._state.current_state = GuardianState.CONFIRMED_DEAD
    sm._state.down_alert_sent = True
    sm.mark_waiting_on_update_lock()
    past = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    sm._state.waiting_since = sm._state.wait_alerted_at = past
    dispatcher = MagicMock(send=AsyncMock())
    run = AsyncMock()
    with (
        patch(
            "genesis.guardian.live_drop.update_lock_holder",
            AsyncMock(return_value=(True, "the deploy marker names pid 4242")),
        ),
        patch("genesis.guardian.check._execute_recovery_with_approval", run),
    ):
        await _handle_confirmed_dead(config, sm, dispatcher, MagicMock(), MagicMock())
    assert dispatcher.send.await_count == alerts
    run.assert_not_awaited()  # alert only: nothing is proposed or restarted
    if alerts:
        alert = dispatcher.send.await_args.args[0]
        assert "has waited 3.0 h for update.lock" in alert.title
        assert "pid 4242" in alert.body and "will not restart anything" in alert.body


@pytest.mark.asyncio
async def test_the_two_hour_limit_survives_a_guardian_restart(config, tmp_path):
    """Each tick is a fresh process: the alert time must be saved and read back,
    or a long wait alerts every 30 s (re-alert review SF-2)."""
    from datetime import UTC, datetime, timedelta

    from genesis.guardian.check import _handle_confirmed_dead

    path = tmp_path / "state.json"
    sm = ConfirmationStateMachine(config)
    sm._state.current_state = GuardianState.CONFIRMED_DEAD
    sm._state.down_alert_sent = True
    sm.mark_waiting_on_update_lock()
    past = (datetime.now(UTC) - timedelta(hours=3)).isoformat()
    sm._state.waiting_since = sm._state.wait_alerted_at = past
    sm.save_state(path)
    dispatcher = MagicMock(send=AsyncMock())
    engine = MagicMock(execute=AsyncMock())
    with patch("genesis.guardian.live_drop.update_lock_holder", AsyncMock(return_value=(True, ""))):
        for _tick in range(3):
            sm = ConfirmationStateMachine(config)
            sm.load_state(path)
            await _handle_confirmed_dead(config, sm, dispatcher, MagicMock(), engine)
            sm.save_state(path)
    assert dispatcher.send.await_count == 1
    engine.execute.assert_not_awaited()


@pytest.mark.parametrize(
    "stored",
    [
        {"waiting_since": 5, "wait_alerted_at": ["x"]},
        {"waiting_since": "2026-11-17T10:00:00", "wait_alerted_at": "nope"},
    ],
    ids=["not-strings", "naive-and-garbage"],
)
def test_a_corrupt_wait_record_never_crashes_a_tick(config, stored):
    from datetime import UTC, datetime

    from genesis.guardian.live_drop import WAIT_REALERT_S
    from genesis.guardian.state_machine import StateData

    sm = ConfirmationStateMachine(config)
    sm._state = StateData.from_dict({"waiting_on_update_lock": True, **stored})
    now = datetime.now(UTC)
    sm.wait_realert_due(now, WAIT_REALERT_S)  # must not raise
    sm.waited_s(now)


def test_a_wait_from_an_older_guardian_starts_now_instead_of_alerting(config):
    from datetime import UTC, datetime

    from genesis.guardian.live_drop import WAIT_REALERT_S
    from genesis.guardian.state_machine import StateData

    sm = ConfirmationStateMachine(config)
    sm._state = StateData.from_dict({"waiting_on_update_lock": True})
    assert sm.wait_realert_due(datetime.now(UTC), WAIT_REALERT_S) is False
    assert sm.state.waiting_since is not None


# ── round 4: one proposal-side rule for every proposal ──────────────────────

ALL = list(RecoveryAction)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ALL, ids=[a.value for a in ALL])
@pytest.mark.parametrize("lock", [(True, ""), (None, "")], ids=["held", "unreadable-running"])
async def test_every_proposal_waits_while_the_lock_is_held_on_live(config, action, lock):
    """Owner ruling (ego 3d02c11f): on `live`, while update.lock is held, every
    proposal, ESCALATE included (a fresh episode's cc_unavailable fallback), is
    the escalation that waits: no Gate 1 prompt, its own warning, the wait marked."""
    from dataclasses import replace

    sm = ConfirmationStateMachine(config)
    sm._state.current_state = GuardianState.CONFIRMED_DEAD
    sm.mark_down_alert_sent()
    channel = _mock_gate_channel(["APPROVE", "APPROVE"])
    engine = _real_engine(config, sm)
    diagnosis = replace(_proposed_diagnosis(action), source="cc_unavailable")
    with (
        patch("genesis.guardian.check._find_telegram_channel", return_value=channel),
        patch("genesis.guardian.recovery.container_on_live", AsyncMock(return_value=True)),
        patch("genesis.guardian.live_drop.update_lock_holder", AsyncMock(return_value=lock)),
        patch("genesis.guardian.live_drop.container_stopped", AsyncMock(return_value=False)),
    ):
        await _execute_recovery_with_approval(config, sm, MagicMock(), engine, diagnosis)
    channel.send_text.assert_not_awaited()  # no Gate 1 prompt
    alert = engine._dispatcher.send.await_args.args[0]
    if lock[0] is None:  # architect LOW 4: nothing shows a deploy
        assert alert.title == "Genesis down — update.lock cannot be read; waiting"
        assert "likely hung" not in alert.body and "asks your approval" in alert.body
    else:
        assert alert.title == "Genesis down — waiting for a deploy to finish"
    assert sm.state.waiting_on_update_lock is True and sm.state.down_alert_sent is True


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ALL, ids=[a.value for a in ALL])
async def test_a_lock_taken_during_gate_1_holds_the_new_proposal(config, action):
    """The lock is read again after the re-diagnosis that follows Gate 1,
    before any redirect (CodeRabbit c4619151366: the git-unhealthy redirect to
    SNAPSHOT_ROLLBACK skipped it)."""
    git = AsyncMock(return_value=False)
    executed, gate2, _ = await _propose(
        config,
        action,
        view=_view(),
        lock=[(False, ""), (True, "")],
        proposed=RecoveryAction.RESTART_SERVICES,
        git=git,
    )
    assert executed.recommended_action == RecoveryAction.ESCALATE
    assert executed.escalation == WAITING_ON_LOCK and gate2 == ""
    git.assert_not_awaited()  # read before any redirect (architect r4 LOW 8)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stopped", "waits"), [(True, False), (False, True)], ids=["stopped", "running"]
)
async def test_an_unreadable_probe_after_gate_1(config, stopped, waits):
    executed, _, _ = await _propose(
        config,
        RecoveryAction.REVERT_CODE,
        lock=(None, ""),
        git_ok=False,
        stopped=stopped,
    )
    want = RecoveryAction.ESCALATE if waits else RecoveryAction.SNAPSHOT_ROLLBACK
    assert executed.recommended_action == want


def _long_wait(config, *, seen_held: bool, hours: float = 3):
    from datetime import UTC, datetime, timedelta

    sm = ConfirmationStateMachine(config)
    sm._state.current_state = GuardianState.CONFIRMED_DEAD
    sm._state.down_alert_sent = True
    sm.mark_waiting_on_update_lock()
    past = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
    sm._state.waiting_since = sm._state.wait_alerted_at = past
    sm._state.wait_lock_seen_held = seen_held
    return sm


async def _tick(config, sm, lock):
    from genesis.guardian.check import _handle_confirmed_dead

    dispatcher = MagicMock(send=AsyncMock())
    run = AsyncMock()
    engine = MagicMock(
        diagnose=AsyncMock(return_value=_proposed_diagnosis(RecoveryAction.RESTART_SERVICES))
    )
    with (
        patch("genesis.guardian.live_drop.update_lock_holder", AsyncMock(return_value=lock)),
        patch("genesis.guardian.live_drop.container_stopped", AsyncMock(return_value=False)),
        patch("genesis.guardian.check.collect_diagnostics", AsyncMock(return_value={})),
        patch("genesis.guardian.check.write_diagnosis_result"),
        patch("genesis.guardian.check._execute_recovery_with_approval", run),
    ):
        await _handle_confirmed_dead(config, sm, dispatcher, engine, MagicMock())
    bodies = [c.args[0].body for c in dispatcher.send.await_args_list]
    return run, bodies


@pytest.mark.asyncio
async def test_two_hours_of_unreadable_probes_reopen_into_the_gates(config):
    """Architect r4 SHOULD-FIX: a wait whose probes cannot say, on a running
    container, otherwise has no exit. Past 2 h with no probe reading the lock
    held, the episode reopens into Gate 1 (the owner decides; nothing runs on
    elapsed time), and the alert says the lock cannot be read."""
    sm = _long_wait(config, seen_held=False)
    run, bodies = await _tick(config, sm, (None, ""))
    run.assert_awaited_once()  # diagnosed again, through both gates
    assert len(bodies) == 1 and "update.lock cannot be read" in bodies[0]
    assert "asks your approval" in bodies[0] and "end that run" not in bodies[0]
    assert sm.state.waiting_on_update_lock is False and sm.state.down_alert_sent is True


@pytest.mark.asyncio
async def test_an_interval_with_a_held_read_keeps_waiting(config):
    """A probe read the lock held since the last alert: a run held it, so the
    wait goes on, alert only, and the text says the lock cannot be read now."""
    sm = _long_wait(config, seen_held=True)
    run, bodies = await _tick(config, sm, (None, ""))
    run.assert_not_awaited()
    assert len(bodies) == 1 and "cannot be read now" in bodies[0]
    assert "end that run" not in bodies[0]
    assert sm.state.waiting_on_update_lock is True
    assert sm.state.wait_lock_seen_held is False  # the alert starts a new interval


@pytest.mark.asyncio
async def test_a_held_read_in_the_tick_counts_for_the_interval(config):
    sm = _long_wait(config, seen_held=False)
    run, bodies = await _tick(config, sm, (True, "the deploy marker names pid 4242"))
    run.assert_not_awaited()
    assert "now held (the deploy marker names pid 4242)" in bodies[0]
    assert sm.state.waiting_on_update_lock is True


@pytest.mark.asyncio
async def test_unreadable_probes_before_two_hours_keep_waiting_quietly(config):
    sm = _long_wait(config, seen_held=False, hours=1)
    run, bodies = await _tick(config, sm, (None, ""))
    run.assert_not_awaited()
    assert bodies == [] and sm.state.waiting_on_update_lock is True


@pytest.mark.parametrize(
    ("stored", "seen"),
    [({}, True), ({"wait_lock_seen_held": "x"}, True), ({"wait_lock_seen_held": False}, False)],
    ids=["older-file", "corrupt", "saved"],
)
def test_the_held_seen_flag_is_saved_and_reads_safe(config, stored, seen):
    """Missing or corrupt reads as held: alert only, never a spurious reopen."""
    from genesis.guardian.state_machine import StateData

    data = StateData.from_dict({"waiting_on_update_lock": True, **stored})
    assert data.wait_lock_seen_held is seen
    assert StateData.from_dict(data.to_dict()).wait_lock_seen_held is seen
    sm = ConfirmationStateMachine(config)
    sm._state = data
    sm.clear_down_alert_sent()
    sm.mark_waiting_on_update_lock()
    assert sm.state.wait_lock_seen_held is False  # a new wait starts its own interval


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("answer", "stopped"),
    [
        ((0, "Name: genesis\nStatus: STOPPED\nType: container", ""), True),
        ((0, "Name: genesis\nStatus: FROZEN\nType: container", ""), False),
        ((0, "Name: genesis\nStatus: RUNNING\n", ""), False),
        ((1, "", "Error: not found"), False),
        ((-1, "", "timeout"), False),
    ],
    ids=["stopped", "frozen", "running", "error", "timeout"],
)
async def test_only_a_reported_stop_counts_as_stopped(answer, stopped):
    """Architect r4 LOW 2: a frozen guest keeps its locks, and a failed or
    slow `incus info` reports nothing about them."""
    from genesis.guardian.live_drop import container_stopped

    async def box(*args, **kwargs):
        return answer

    with patch("genesis.guardian.live_drop._run_subprocess", box):
        assert await container_stopped("genesis") is stopped


# ── the 2 h unreadable-only reopen, composed end to end (architect delta review) ──


async def _reopen_episode(config, lock_answers):
    """A 3 h wait whose probes all read undecided, ticked through the REAL
    _handle_confirmed_dead, _execute_recovery_with_approval and RecoveryEngine.
    ``lock_answers(n)`` answers the n-th probe. Returns (Gate 1 prompted, the
    action run or None, the state machine)."""
    from genesis.guardian.check import _handle_confirmed_dead

    sm = _long_wait(config, seen_held=False)
    engine = _real_engine(config, sm)
    channel = _mock_gate_channel(["APPROVE", "APPROVE"])
    calls = {"n": 0}

    async def probe(container):
        calls["n"] += 1
        return lock_answers(calls["n"])

    ran = AsyncMock(return_value=(True, "Container restarted"))
    healthy = _dead_snapshot().__class__(
        signals={"health_api": _dead_snapshot().signals["heartbeat_canary"]},
        pause_state=_dead_snapshot().pause_state,
    )
    proposal = AsyncMock(return_value=_proposed_diagnosis(RecoveryAction.RESTART_CONTAINER))
    with (
        patch("genesis.guardian.live_drop.update_lock_holder", probe),
        patch("genesis.guardian.recovery.update_lock_holder", probe),
        patch("genesis.guardian.live_drop.container_stopped", AsyncMock(return_value=False)),
        patch("genesis.guardian.recovery.container_stopped", AsyncMock(return_value=False)),
        patch("genesis.guardian.recovery.container_on_live", AsyncMock(return_value=True)),
        patch("genesis.guardian.check._find_telegram_channel", return_value=channel),
        patch(
            "genesis.guardian.check.collect_all_signals", AsyncMock(return_value=_dead_snapshot())
        ),
        patch("genesis.guardian.check.collect_diagnostics", AsyncMock(return_value={})),
        patch("genesis.guardian.check.write_diagnosis_result"),
        patch.object(DiagnosisEngine, "diagnose", proposal),
        patch("genesis.guardian.recovery.collect_all_signals", AsyncMock(return_value=healthy)),
        patch.object(engine, "_execute_action", ran),
        patch.object(engine._snapshots, "safe_to_snapshot", AsyncMock(return_value=False)),
    ):
        await _handle_confirmed_dead(
            config, sm, engine._dispatcher, MagicMock(diagnose=proposal), engine
        )
    texts = [str(c.args[0]) for c in channel.send_text.await_args_list]
    prompted = any("Genesis down" in t and "APPROVE" in t for t in texts)
    action = ran.await_args.args[0] if ran.await_args else None
    gate2 = any("Proposed:" in t for t in texts)
    return prompted, action, sm, gate2


@pytest.mark.asyncio
async def test_the_unreadable_reopen_reaches_the_gates_and_restarts(config):
    """BLOCKER (delta review): after 2 h of only-unreadable probes the episode
    must reach Gate 1, and once approved the restart runs with the probe still
    unreadable. Before the fix the reopen went straight back into the wait."""
    prompted, action, sm, gate2 = await _reopen_episode(config, lambda n: (None, ""))
    assert prompted, "Gate 1 was never prompted"
    assert action == RecoveryAction.RESTART_CONTAINER
    assert sm.state.waiting_on_update_lock is False


@pytest.mark.asyncio
async def test_a_held_read_after_the_unreadable_reopen_still_waits(config):
    """The reopen relaxes only the undecided read: a lock read HELD anywhere
    in that chain (here after Gate 1) is still the wait."""
    prompted, action, sm, gate2 = await _reopen_episode(
        config, lambda n: (None, "") if n <= 2 else (True, "")
    )
    assert prompted  # the tick and the entry read undecided: Gate 1 asked
    assert not gate2  # held at the proposal, not only at execute(): no Gate 2 prompt
    assert action is None
    assert sm.state.waiting_on_update_lock is True


@pytest.mark.asyncio
async def test_a_stopped_instance_reaches_gate_1_through_the_real_probe(config):
    """Architect LOW 6, the 2026-07-04 class through check.py: incus exec cannot
    answer a stopped instance and info reports STOPPED; the real probe and the
    real stopped check let the RESTART_CONTAINER proposal reach the gates."""

    async def host(*args, **kwargs):
        if args[:2] == ("incus", "info"):
            return (0, "Name: genesis\nStatus: STOPPED\n", "")
        if args[:2] == ("incus", "exec"):
            return (1, "", "Error: Instance is not running")
        return (0, "", "")

    sm = ConfirmationStateMachine(config)
    sm._state.current_state = GuardianState.CONFIRMED_DEAD
    sm.mark_down_alert_sent()
    channel = _mock_gate_channel(["APPROVE", "APPROVE"])
    recovery_engine = MagicMock(execute=AsyncMock())
    with (
        patch("genesis.guardian.live_drop._run_subprocess", host),
        patch("genesis.guardian.recovery._run_subprocess", host),
        patch("genesis.guardian.check._find_telegram_channel", return_value=channel),
        patch(
            "genesis.guardian.check.collect_all_signals", AsyncMock(return_value=_dead_snapshot())
        ),
        patch("genesis.guardian.check.collect_diagnostics", AsyncMock(return_value={})),
        patch("genesis.guardian.check.write_diagnosis_result"),
        patch.object(
            DiagnosisEngine,
            "diagnose",
            AsyncMock(return_value=_proposed_diagnosis(RecoveryAction.RESTART_CONTAINER)),
        ),
    ):
        await _execute_recovery_with_approval(
            config,
            sm,
            MagicMock(send=AsyncMock()),
            recovery_engine,
            _proposed_diagnosis(RecoveryAction.RESTART_CONTAINER),
        )
    texts = [str(c.args[0]) for c in channel.send_text.await_args_list]
    assert any("Genesis down" in t and "APPROVE" in t for t in texts), texts
    executed = recovery_engine.execute.await_args.args[0]
    assert executed.recommended_action == RecoveryAction.RESTART_CONTAINER


@pytest.mark.asyncio
async def test_a_free_reopen_keeps_the_ruling(config):
    """The relaxed read is scoped to the unreadable-only reopen: an episode
    reopened because the lock read FREE, whose next probe is undecided on a
    running container, waits again (owner ruling ego 9cbdc406)."""
    prompted, action, sm, gate2 = await _reopen_episode(
        config, lambda n: (False, "") if n == 1 else (None, "")
    )
    assert not prompted and action is None
    assert sm.state.waiting_on_update_lock is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("lock", "waits"), [((None, ""), False), ((True, ""), True)], ids=["unreadable", "held"]
)
async def test_hold_for_lock_without_unread_holds(lock, waits):
    from genesis.guardian.live_drop import hold_for_lock

    stopped = AsyncMock(return_value=False)
    with (
        patch("genesis.guardian.live_drop.update_lock_holder", AsyncMock(return_value=lock)),
        patch("genesis.guardian.live_drop.container_stopped", stopped),
        patch("genesis.guardian.recovery.container_on_live", AsyncMock(return_value=True)),
    ):
        out = await hold_for_lock(
            "genesis", _proposed_diagnosis(RecoveryAction.RESTART_CONTAINER), unread_holds=False
        )
    assert (out.recommended_action == RecoveryAction.ESCALATE) is waits
    stopped.assert_not_awaited()  # no `incus info` when undecided no longer holds
