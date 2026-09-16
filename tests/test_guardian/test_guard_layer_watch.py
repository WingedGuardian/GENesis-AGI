"""Tests for the guardian-side guard-layer watch.

The watch asks whether the AGENT TOOLING can still evaluate — the CC hook
launcher, the modules its guards import, the interpreter they run on, and the
binary the host's recovery brain launches. It is ALERT-ONLY: it takes no action.

Covers the pure parse + decide functions, the episode lifecycle (including the
two leaks an adversarial audit reproduced), the three-valued host probe, and the
orchestrator. No test touches a real container, matching every other watch here.
"""

from __future__ import annotations

import ast
import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from genesis.guardian import check as check_mod
from genesis.guardian import guard_layer_watch as glw
from genesis.guardian.alert.base import AlertSeverity
from genesis.guardian.config import GuardianConfig, GuardLayerConfig


class _Cfg:
    """Minimal config stub exposing what guard_layer_watch reads."""

    def __init__(self, tmp_path):
        self.container_name = "genesis"
        self.guard_layer = GuardLayerConfig()
        self.cc = GuardianConfig().cc
        self._sp = tmp_path

    @property
    def state_path(self):
        return self._sp


@pytest.fixture(autouse=True)
def _on_a_guardian_host(monkeypatch):
    """Every test here runs as though `incus` is present.

    The watch is gated on being a guardian host, and the suite runs in the
    CONTAINER, which has no `incus` — so without this every orchestrator test
    would return early and pass vacuously. TestNotAGuardianHost overrides it
    deliberately, which is the only place the absence is the subject.
    """
    monkeypatch.setattr(glw.shutil, "which", lambda name: f"/usr/bin/{name}")


def _now():
    return datetime(2026, 9, 16, 12, 0, 0, tzinfo=UTC)


def _healthy():
    return {"failures": []}


def _failing(*conditions):
    return {"failures": list(conditions)}


# ── Parsing: unparseable must mean NO SIGNAL, never a false alert ─────────────


class TestParseProbe:
    def test_healthy(self):
        assert glw._parse_probe("login banner\nGUARDLAYER ok\n") == {"failures": []}

    def test_failures_are_split(self):
        got = glw._parse_probe("GUARDLAYER venv_dead node_dead\n")
        assert got["failures"] == ["venv_dead", "node_dead"]

    def test_no_marker_is_no_signal(self):
        """A probe we cannot read is silence, not a finding."""
        assert glw._parse_probe("bash: command not found\n") is None

    def test_empty_output_is_no_signal(self):
        assert glw._parse_probe("") is None


# ── The escalation ladder ────────────────────────────────────────────────────


class TestDecide:
    def _cfg(self):
        return GuardLayerConfig()

    def test_healthy_with_no_episode_is_silent(self):
        assert glw.decide("venv_dead", False, None, _now(), self._cfg()).action == "none"

    def test_healthy_after_a_warning_resolves(self):
        ep = {"warned_at": _now().isoformat()}
        assert glw.decide("venv_dead", False, ep, _now(), self._cfg()).action == "resolved"

    def test_healthy_before_any_warning_does_not_resolve(self):
        """Nothing was ever announced, so there is nothing to announce recovery from."""
        assert (
            glw.decide("venv_dead", False, {"consecutive": 1}, _now(), self._cfg()).action == "none"
        )

    def test_first_failure_only_confirms(self):
        """confirm_ticks absorbs a blip — e.g. a deploy rebuilding the venv."""
        assert (
            glw.decide("venv_dead", True, {"consecutive": 1}, _now(), self._cfg()).action == "none"
        )

    def test_confirmed_failure_warns(self):
        assert (
            glw.decide("venv_dead", True, {"consecutive": 2}, _now(), self._cfg()).action == "warn"
        )

    def test_realert_is_damped_inside_the_window(self):
        ep = {
            "consecutive": 9,
            "warned_at": _now().isoformat(),
            "last_alert_at": _now().isoformat(),
        }
        assert glw.decide("venv_dead", True, ep, _now(), self._cfg()).action == "none"

    def test_realert_fires_once_the_window_elapses(self):
        cfg = self._cfg()
        old = (_now() - timedelta(hours=cfg.realert_hours + 1)).isoformat()
        ep = {"consecutive": 9, "warned_at": old, "last_alert_at": old}
        assert glw.decide("venv_dead", True, ep, _now(), cfg).action == "realert"


# ── The host probe's THREE values ────────────────────────────────────────────


class TestProbeHostBrain:
    @pytest.mark.asyncio
    async def test_a_missing_binary_is_a_definite_negative(self, tmp_path):
        cfg = _Cfg(tmp_path)
        cfg.cc = type("C", (), {"path": str(tmp_path / "no-such-claude")})()
        assert await glw.probe_host_brain(cfg) is False

    @pytest.mark.asyncio
    async def test_a_working_binary_is_a_definite_positive(self, tmp_path):
        cfg = _Cfg(tmp_path)
        cfg.cc = type("C", (), {"path": "/bin/true"})()
        assert await glw.probe_host_brain(cfg) is True

    @pytest.mark.asyncio
    async def test_a_nonzero_exit_is_a_definite_negative(self, tmp_path):
        cfg = _Cfg(tmp_path)
        cfg.cc = type("C", (), {"path": "/bin/false"})()
        assert await glw.probe_host_brain(cfg) is False

    @pytest.mark.asyncio
    async def test_a_wedge_is_INCONCLUSIVE_not_dead(self, tmp_path):
        """A hung probe says nothing about whether the brain works.

        Also exercises the reap path: the timeout must kill the process GROUP and
        wait for it, or `claude`'s node children outlive every tick.
        """
        cfg = _Cfg(tmp_path)
        cfg.guard_layer = GuardLayerConfig(check_timeout_s=1)
        cfg.cc = type("C", (), {"path": "/bin/sleep"})()
        # /bin/sleep with no args exits immediately; use a wrapper that hangs.
        script = tmp_path / "hang"
        script.write_text("#!/bin/sh\nsleep 30\n")
        script.chmod(0o755)
        cfg.cc = type("C", (), {"path": str(script)})()
        assert await glw.probe_host_brain(cfg) is None


# ── Orchestrator ─────────────────────────────────────────────────────────────


class TestOrchestrator:
    @pytest.mark.asyncio
    async def test_unreachable_probe_sends_nothing(self, tmp_path, monkeypatch):
        """A down container is the state machine's job, not an alert from here."""
        monkeypatch.setattr(glw, "probe_guard_layer", AsyncMock(return_value=None))
        monkeypatch.setattr(glw, "probe_host_brain", AsyncMock(return_value=True))
        disp = AsyncMock()
        await glw.check_guard_layer_and_alert(_Cfg(tmp_path), disp)
        disp.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_confirmed_failure_warns(self, tmp_path, monkeypatch):
        monkeypatch.setattr(glw, "probe_guard_layer", AsyncMock(return_value=_failing("venv_dead")))
        monkeypatch.setattr(glw, "probe_host_brain", AsyncMock(return_value=True))
        cfg, disp = _Cfg(tmp_path), AsyncMock()
        for _ in range(cfg.guard_layer.confirm_ticks):
            await glw.check_guard_layer_and_alert(cfg, disp)
        assert disp.send.call_count == 1
        alert = disp.send.call_args.args[0]
        assert alert.severity is AlertSeverity.WARNING
        assert "venv_dead" in alert.title
        assert "bootstrap.sh" in alert.body, "an alert must name the repair route"

    @pytest.mark.asyncio
    async def test_the_launcher_condition_is_reachable(self, tmp_path, monkeypatch):
        """The launcher is what CC actually invokes; a venv check alone misses it."""
        monkeypatch.setattr(
            glw, "probe_guard_layer", AsyncMock(return_value=_failing("hook_launcher_dead"))
        )
        monkeypatch.setattr(glw, "probe_host_brain", AsyncMock(return_value=True))
        cfg, disp = _Cfg(tmp_path), AsyncMock()
        for _ in range(cfg.guard_layer.confirm_ticks):
            await glw.check_guard_layer_and_alert(cfg, disp)
        assert "hook_launcher_dead" in disp.send.call_args.args[0].title

    @pytest.mark.asyncio
    async def test_recovery_sends_an_INFO_and_clears_state(self, tmp_path, monkeypatch):
        cfg, disp = _Cfg(tmp_path), AsyncMock()
        monkeypatch.setattr(glw, "probe_host_brain", AsyncMock(return_value=True))
        monkeypatch.setattr(glw, "probe_guard_layer", AsyncMock(return_value=_failing("venv_dead")))
        for _ in range(cfg.guard_layer.confirm_ticks):
            await glw.check_guard_layer_and_alert(cfg, disp)
        disp.reset_mock()
        monkeypatch.setattr(glw, "probe_guard_layer", AsyncMock(return_value=_healthy()))
        await glw.check_guard_layer_and_alert(cfg, disp)
        assert disp.send.call_args.args[0].severity is AlertSeverity.INFO
        assert glw._load_state(tmp_path / glw._STATE_FILE) == {}

    @pytest.mark.asyncio
    async def test_a_blip_that_never_warned_leaves_NO_state_behind(self, tmp_path, monkeypatch):
        """REGRESSION (audit BLOCKER-1): a leaked episode strands state forever.

        In the draft that carried a grace window it was worse than untidy — the
        stale `first_seen` made the window instantly expired on the next
        occurrence, so the destructive step ran one tick after the first warning
        instead of ten minutes later. The verb is gone; the leak is fixed anyway,
        because an episode nobody clears is state that grows without bound.
        """
        cfg, disp = _Cfg(tmp_path), AsyncMock()
        monkeypatch.setattr(glw, "probe_host_brain", AsyncMock(return_value=True))
        monkeypatch.setattr(
            glw, "probe_guard_layer", AsyncMock(return_value=_failing("hook_input_broken"))
        )
        await glw.check_guard_layer_and_alert(cfg, disp)  # one failing tick, below confirm
        assert "hook_input_broken" in glw._load_state(tmp_path / glw._STATE_FILE)

        monkeypatch.setattr(glw, "probe_guard_layer", AsyncMock(return_value=_healthy()))
        await glw.check_guard_layer_and_alert(cfg, disp)
        assert glw._load_state(tmp_path / glw._STATE_FILE) == {}, (
            "an episode that never reached a warning must be cleared when it goes "
            "healthy, not left on disk with a start timestamp nobody refreshes"
        )
        disp.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_inconclusive_host_probe_never_emits_a_false_recovery(
        self, tmp_path, monkeypatch
    ):
        """REGRESSION (audit SHOULD-FIX-4): inconclusive must not read as healthy.

        The persisted episode is re-admitted by `set(known) | set(episodes)`, so
        without an explicit skip the condition falls through the not-failing branch
        and resolves — telling the operator it recovered when nothing was observed,
        and resetting a ladder that is still climbing.
        """
        cfg, disp = _Cfg(tmp_path), AsyncMock()
        monkeypatch.setattr(glw, "probe_guard_layer", AsyncMock(return_value=_healthy()))
        monkeypatch.setattr(glw, "probe_host_brain", AsyncMock(return_value=False))
        for _ in range(cfg.guard_layer.confirm_ticks):
            await glw.check_guard_layer_and_alert(cfg, disp)
        assert disp.send.call_args.args[0].severity is AlertSeverity.WARNING
        disp.reset_mock()

        monkeypatch.setattr(glw, "probe_host_brain", AsyncMock(return_value=None))
        await glw.check_guard_layer_and_alert(cfg, disp)
        disp.send.assert_not_called()
        assert "host_brain_dead" in glw._load_state(tmp_path / glw._STATE_FILE), (
            "an inconclusive probe must leave the episode intact so the ladder can "
            "still escalate; clearing it makes escalation unreachable"
        )

    @pytest.mark.asyncio
    async def test_a_probe_that_raises_never_reaches_the_tick(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            glw, "probe_guard_layer", AsyncMock(side_effect=RuntimeError("probe exploded"))
        )
        monkeypatch.setattr(glw, "probe_host_brain", AsyncMock(return_value=True))
        await glw.check_guard_layer_and_alert(_Cfg(tmp_path), AsyncMock())

    @pytest.mark.asyncio
    async def test_a_failing_DISPATCHER_never_reaches_the_tick(self, tmp_path, monkeypatch):
        """Distinct from the probe case: the alert channel itself is what fails."""
        monkeypatch.setattr(glw, "probe_guard_layer", AsyncMock(return_value=_failing("node_dead")))
        monkeypatch.setattr(glw, "probe_host_brain", AsyncMock(return_value=True))
        cfg = _Cfg(tmp_path)
        disp = AsyncMock()
        disp.send = AsyncMock(side_effect=RuntimeError("telegram down"))
        for _ in range(cfg.guard_layer.confirm_ticks):
            await glw.check_guard_layer_and_alert(cfg, disp)

    @pytest.mark.asyncio
    async def test_the_kill_switch_disables_everything(self, tmp_path, monkeypatch):
        probe = AsyncMock(return_value=_failing("venv_dead"))
        monkeypatch.setattr(glw, "probe_guard_layer", probe)
        cfg, disp = _Cfg(tmp_path), AsyncMock()
        cfg.guard_layer = GuardLayerConfig(enabled=False)
        await glw.check_guard_layer_and_alert(cfg, disp)
        probe.assert_not_called()
        disp.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_corrupt_state_on_disk_degrades_to_empty_rather_than_crashing(
        self, tmp_path, monkeypatch
    ):
        (tmp_path / glw._STATE_FILE).write_text("{not json")
        monkeypatch.setattr(glw, "probe_guard_layer", AsyncMock(return_value=_healthy()))
        monkeypatch.setattr(glw, "probe_host_brain", AsyncMock(return_value=True))
        await glw.check_guard_layer_and_alert(_Cfg(tmp_path), AsyncMock())


# ── The module takes no action, and that is a property worth locking ─────────


def test_the_watch_has_no_path_to_the_recovery_engine():
    """A broken hook file must never be able to restart the container.

    Asserted over the module SOURCE rather than by mocking, because the failure
    this catches is someone later importing the recovery machinery to "just fix
    it" — which no behavioural test would notice until it fired in production.
    """
    # Imports are the BINDING surface. A prose mention proves nothing either way,
    # and a substring scan over the whole source fires on the docstring that
    # explains the design - so only the import lines are examined.
    src = Path(glw.__file__).read_text()
    import_lines = [ln for ln in src.splitlines() if ln.startswith(("import ", "from "))]
    joined = " ".join(import_lines)
    for forbidden in ("guardian.recovery", "guardian.state_machine", "guardian.snapshots"):
        assert forbidden not in joined, (
            f"guard_layer_watch imports {forbidden} — this watch must have NO path to a "
            f"recovery action. A broken hook file must not be able to restart the container."
        )


def test_the_module_performs_no_container_writes():
    """Alert-only is the shipped posture; the repair verb was removed, not disabled.

    A disabled-by-default destructive path is still one config edit from running,
    so its ABSENCE is what gets locked.

    Asserted over the AST and over the executed byte literals, NOT over the raw
    source: a substring scan fires on the module docstring that explains why the
    verb was removed. Existence of a phrase is not the property — a defined
    function or an executed command is.
    """
    tree = ast.parse(Path(glw.__file__).read_text())

    defined = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    } | {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    for name in ("run_repair", "_REPAIR_SCRIPT", "_repair_is_in_scope"):
        assert name not in defined, (
            f"{name} is defined — this watch is alert-only. Reintroducing a repair verb "
            f"needs its own review: the previous one was REPRODUCED destroying staged "
            f"content (git checkout overwrites the index) and silently resolving a merge "
            f"conflict while MERGE_HEAD remained."
        )

    # Every bytes literal in the module is a payload that gets EXECUTED in the
    # container, so a mutating git verb inside one is a write no matter what the
    # surrounding prose says.
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, bytes):
            payload = node.value.decode("utf-8", "replace")
            for verb in ("git checkout", "git restore", "git reset", "git stash", "rm "):
                assert verb not in payload, (
                    f"an executed payload contains {verb!r} — this watch must not write "
                    f"into the container."
                )


# ── Wiring ───────────────────────────────────────────────────────────────────


def test_run_check_wires_the_watch():
    """Cheap insurance against the module existing but never being called.

    Reads check.py's own source rather than mocking, because the failure this
    catches is the call site being deleted or commented out — which no mock would
    notice.
    """
    lines = Path(check_mod.__file__).read_text().splitlines()
    call = [ln for ln in lines if "_check_guard_layer_and_alert(config, dispatcher)" in ln]
    assert call, "the run_check call site is gone"
    assert any(not ln.lstrip().startswith("#") for ln in call), (
        "the only call site is commented out — wired in source, inert at runtime"
    )
    assert (
        "from genesis.guardian.guard_layer_watch import check_guard_layer_and_alert"
        in "\n".join(lines)
    )


def test_config_defaults_present():
    cfg = GuardianConfig()
    assert isinstance(cfg.guard_layer, GuardLayerConfig)
    assert cfg.guard_layer.enabled is True


def test_the_probe_payload_is_self_contained():
    """Piped to `bash -s` as bytes, and it must report ON the venv.

    So it cannot import the genesis package, and it cannot interpolate anything —
    a static literal is what makes the "no shell quoting exposure" claim true.
    """
    script = glw._PROBE_SCRIPT
    assert isinstance(script, bytes)
    assert b"genesis.guardian" not in script
    assert b"import genesis" not in script
    assert b"%s" not in script and b"{}" not in script.replace(b"echo '{}'", b"")


def test_the_probe_covers_every_condition_it_can_report():
    """Every CONDITION_* the container probe can emit must be produced by the script.

    Catches a condition constant that drifts out of the bash payload — the two are
    separate languages, so nothing else connects them.
    """
    script = glw._PROBE_SCRIPT.decode()
    for condition in glw._CONTAINER_CONDITIONS:
        assert condition in script, f"{condition} is declared but the probe never emits it"
        assert condition in glw._CONDITION_DETAIL, f"{condition} has no operator-facing detail"
    assert glw.CONDITION_HOST_BRAIN in glw._CONDITION_DETAIL


# ── Regressions for the four Codex P2s (#2092) ───────────────────────────────


class TestHostLegIndependence:
    """The host probe is host-local; the container's reachability must not gate it."""

    @pytest.mark.asyncio
    async def test_an_unreachable_container_does_not_suppress_the_host_brain(
        self, tmp_path, monkeypatch
    ):
        """P2-1. This is the moment the recovery brain matters MOST.

        The old early-return on an unreachable container skipped the host leg
        entirely, so a container that is down WHILE the configured Claude binary is
        broken reported nothing about the second failure — the one that would have
        fixed the first.
        """
        monkeypatch.setattr(glw, "probe_guard_layer", AsyncMock(return_value=None))
        monkeypatch.setattr(glw, "probe_host_brain", AsyncMock(return_value=False))
        cfg, disp = _Cfg(tmp_path), AsyncMock()
        for _ in range(cfg.guard_layer.confirm_ticks):
            await glw.check_guard_layer_and_alert(cfg, disp)
        assert disp.send.call_count == 1
        assert "host_brain_dead" in disp.send.call_args.args[0].title

    @pytest.mark.asyncio
    async def test_an_unreachable_container_never_resolves_a_container_condition(
        self, tmp_path, monkeypatch
    ):
        """No container evidence is INCONCLUSIVE, not healthy.

        Otherwise a down container would emit a false "recovered" for every
        container condition that was mid-ladder.
        """
        monkeypatch.setattr(glw, "probe_host_brain", AsyncMock(return_value=True))
        monkeypatch.setattr(glw, "probe_guard_layer", AsyncMock(return_value=_failing("venv_dead")))
        cfg, disp = _Cfg(tmp_path), AsyncMock()
        for _ in range(cfg.guard_layer.confirm_ticks):
            await glw.check_guard_layer_and_alert(cfg, disp)
        disp.reset_mock()
        monkeypatch.setattr(glw, "probe_guard_layer", AsyncMock(return_value=None))
        await glw.check_guard_layer_and_alert(cfg, disp)
        disp.send.assert_not_called()
        assert "venv_dead" in glw._load_state(tmp_path / glw._STATE_FILE)


class TestHostBrainStateResolution:
    def test_a_disabled_cc_is_inconclusive_forever(self, tmp_path):
        """P2-3. `cc.enabled: false` is a SUPPORTED configuration, not a fault.

        install_guardian.sh writes it when Claude is absent and DiagnosisEngine
        skips CC for the same reason, so alerting would be a recurring false alarm
        about a component nobody wants running.
        """
        cfg = _Cfg(tmp_path)
        cfg.cc = type("C", (), {"path": "claude", "enabled": False})()
        assert glw.host_brain_state(cfg, False, None) == "inconclusive"
        assert glw.host_brain_state(cfg, None, {"wedged": 99}) == "inconclusive"

    def test_one_wedge_is_inconclusive(self, tmp_path):
        cfg = _Cfg(tmp_path)
        assert glw.host_brain_state(cfg, None, {"wedged": 1}) == "inconclusive"

    def test_a_PERSISTENT_wedge_escalates(self, tmp_path):
        """P2-2. Unavailable in practice is unavailable.

        A binary that never answers within its timeout leaves the recovery brain
        operationally dead; treating every wedge as inconclusive left that state
        silent forever.
        """
        cfg = _Cfg(tmp_path)
        assert glw.host_brain_state(cfg, None, {"wedged": cfg.guard_layer.confirm_ticks}) == "failing"

    def test_a_definite_answer_outranks_any_streak(self, tmp_path):
        cfg = _Cfg(tmp_path)
        assert glw.host_brain_state(cfg, True, {"wedged": 99}) == "healthy"
        assert glw.host_brain_state(cfg, False, {"wedged": 0}) == "failing"

    @pytest.mark.asyncio
    async def test_a_persistent_wedge_reaches_an_ALERT_end_to_end(self, tmp_path, monkeypatch):
        """The unit rule above is only useful if the orchestrator carries the streak."""
        monkeypatch.setattr(glw, "probe_guard_layer", AsyncMock(return_value=_healthy()))
        monkeypatch.setattr(glw, "probe_host_brain", AsyncMock(return_value=None))
        cfg, disp = _Cfg(tmp_path), AsyncMock()
        for _ in range(cfg.guard_layer.confirm_ticks * 2 + 1):
            await glw.check_guard_layer_and_alert(cfg, disp)
        assert disp.send.call_count >= 1, "a permanently wedged host brain must not stay silent"
        assert "host_brain_dead" in disp.send.call_args.args[0].title

    @pytest.mark.asyncio
    async def test_a_recovered_probe_clears_the_wedge_streak(self, tmp_path, monkeypatch):
        monkeypatch.setattr(glw, "probe_guard_layer", AsyncMock(return_value=_healthy()))
        monkeypatch.setattr(glw, "probe_host_brain", AsyncMock(return_value=None))
        cfg, disp = _Cfg(tmp_path), AsyncMock()
        await glw.check_guard_layer_and_alert(cfg, disp)
        assert glw._load_state(tmp_path / glw._STATE_FILE)["host_brain_dead"]["wedged"] == 1
        monkeypatch.setattr(glw, "probe_host_brain", AsyncMock(return_value=True))
        await glw.check_guard_layer_and_alert(cfg, disp)
        ep = glw._load_state(tmp_path / glw._STATE_FILE).get("host_brain_dead", {})
        assert "wedged" not in ep


class TestCorruptState:
    """P2-4. Valid JSON of the WRONG SHAPE used to wedge the watch permanently."""

    @pytest.mark.parametrize("payload", ["[]", "null", '"a string"', "42", '{"episodes": []}',
                                         '{"episodes": {"x": "not a dict"}}'])
    def test_structurally_wrong_state_degrades_to_empty(self, tmp_path, payload):
        (tmp_path / glw._STATE_FILE).write_text(payload)
        assert glw._load_state(tmp_path / glw._STATE_FILE) == {} or all(
            isinstance(v, dict) for v in glw._load_state(tmp_path / glw._STATE_FILE).values()
        )

    @pytest.mark.asyncio
    async def test_a_wrong_shaped_state_file_does_not_wedge_the_watch(
        self, tmp_path, monkeypatch
    ):
        """The old `.get` on a list raised AttributeError, which `_load_state` did not
        catch. The outer swallow logged it and left the file in place, so EVERY later
        tick repeated the exception after paying for the probe and no condition was
        ever processed again.
        """
        (tmp_path / glw._STATE_FILE).write_text("[]")
        monkeypatch.setattr(glw, "probe_guard_layer", AsyncMock(return_value=_failing("node_dead")))
        monkeypatch.setattr(glw, "probe_host_brain", AsyncMock(return_value=True))
        cfg, disp = _Cfg(tmp_path), AsyncMock()
        for _ in range(cfg.guard_layer.confirm_ticks):
            await glw.check_guard_layer_and_alert(cfg, disp)
        assert disp.send.call_count == 1, (
            "a structurally corrupt state file must not stop the watch from alerting"
        )


# ── Regressions for the round-3 findings (two P1s + a P2) ────────────────────


def test_the_watch_runs_BEFORE_the_diagnosis_cycle():
    """P1. The host leg must not queue behind the operation it monitors.

    `_check_cycle` can invoke DiagnosisEngine, which runs the host's `claude -p`
    with `cc.timeout_s` (default 3600s) — the SAME binary this watch's host leg
    probes. Sequenced after it, each observation the confirmation ladder needs
    could take an hour, so with the container down and that binary wedged the
    alert arrives late or never: silent in the exact outage it exists for.

    Asserted over source ORDER rather than by mocking, because the failure is a
    call being moved, which no behavioural test in this file would notice.
    """
    text = Path(check_mod.__file__).read_text()
    watch_at = text.index("await _check_guard_layer_and_alert(config, dispatcher)")
    cycle_at = text.index("await _check_cycle(config, sm, dispatcher")
    assert watch_at < cycle_at, (
        "the guard-layer watch is sequenced AFTER _check_cycle, which can block for "
        "cc.timeout_s inside the very binary the host leg probes. Move it before."
    )


def test_every_subcheck_in_the_probe_is_individually_bounded():
    """P1. One hung command must not silence every condition.

    The marker line is printed only after all subchecks finish, so an unbounded
    hang — most pointedly in `genesis-hook`, the thing being monitored — runs out
    the OUTER incus timeout, the probe parses as None, and every container
    condition becomes inconclusive forever rather than reaching confirm_ticks.
    """
    script = glw._PROBE_SCRIPT.decode()
    assert "timeout " in script, "no per-subcheck timeout wrapper in the probe"
    # Each executable subcheck must be wrapped. Counted rather than eyeballed:
    # the wrapper is defined once as $T and must be applied to every command that
    # can hang.
    body = script.split('T="timeout', 1)[1]
    for command in ('"$HOOK"', '"$VENV" -c ""', 'import hook_input',
                    'import shell_parse', "node --version"):
        line = next(ln for ln in body.splitlines() if command in ln)
        assert "$T" in line, (
            f"the subcheck running {command} is not wrapped in $T, so a hang there "
            f"kills the whole probe and silences every other condition"
        )


def test_the_launcher_probe_is_decoupled_from_the_conditions_it_is_not_measuring():
    """P2. Launcher health must not be inferred from a dependency's health.

    Probing the launcher by running `hooks/hook_input.py` through it meant a broken
    hook_input reported BOTH hook_launcher_dead and hook_input_broken — and the
    launcher alert says "the guards are silently off", which is the WRONG POLARITY
    for that failure: a broken hook_input fails CLOSED.
    """
    script = glw._PROBE_SCRIPT.decode()
    launcher_line = next(ln for ln in script.splitlines() if '"$HOOK"' in ln and "$T" in ln)
    assert "hook_input.py" not in launcher_line, (
        "the launcher probe runs hook_input.py, so a broken import is misreported as "
        "a dead launcher — with an alert whose polarity is wrong for that failure"
    )
    assert "nonexistent" in launcher_line, (
        "the launcher probe should use a script name that cannot exist, so what it "
        "measures is whether the LAUNCHER reached its own error handling"
    )


class TestScopedRecovery:
    """P2. A recovery notice must never read as an all-clear it has not earned."""

    @pytest.mark.asyncio
    async def test_recovery_names_what_is_still_degraded(self, tmp_path, monkeypatch):
        monkeypatch.setattr(glw, "probe_host_brain", AsyncMock(return_value=True))
        monkeypatch.setattr(
            glw, "probe_guard_layer",
            AsyncMock(return_value=_failing("node_dead", "hook_input_broken")),
        )
        cfg, disp = _Cfg(tmp_path), AsyncMock()
        for _ in range(cfg.guard_layer.confirm_ticks):
            await glw.check_guard_layer_and_alert(cfg, disp)
        disp.reset_mock()

        # node recovers; hook_input does not.
        monkeypatch.setattr(
            glw, "probe_guard_layer", AsyncMock(return_value=_failing("hook_input_broken"))
        )
        await glw.check_guard_layer_and_alert(cfg, disp)
        bodies = [c.args[0].body for c in disp.send.call_args_list]
        recovery = [b for b in bodies if "cleared" in b]
        assert recovery, "node_dead clearing should send a recovery notice"
        assert "STILL DEGRADED" in recovery[0], (
            "a recovery notice sent while another condition is still warned must say "
            "so — an unqualified all-clear is the most expensive kind of wrong here"
        )
        assert "hook_input_broken" in recovery[0]

    @pytest.mark.asyncio
    async def test_a_true_all_clear_says_so(self, tmp_path, monkeypatch):
        monkeypatch.setattr(glw, "probe_host_brain", AsyncMock(return_value=True))
        monkeypatch.setattr(glw, "probe_guard_layer", AsyncMock(return_value=_failing("node_dead")))
        cfg, disp = _Cfg(tmp_path), AsyncMock()
        for _ in range(cfg.guard_layer.confirm_ticks):
            await glw.check_guard_layer_and_alert(cfg, disp)
        disp.reset_mock()
        monkeypatch.setattr(glw, "probe_guard_layer", AsyncMock(return_value=_healthy()))
        await glw.check_guard_layer_and_alert(cfg, disp)
        assert "healthy again" in disp.send.call_args.args[0].body

    @pytest.mark.asyncio
    async def test_recovery_during_an_inconclusive_tick_is_not_an_all_clear(
        self, tmp_path, monkeypatch
    ):
        """Nothing was observed about the host leg, so nothing may be claimed."""
        monkeypatch.setattr(glw, "probe_host_brain", AsyncMock(return_value=True))
        monkeypatch.setattr(glw, "probe_guard_layer", AsyncMock(return_value=_failing("node_dead")))
        cfg, disp = _Cfg(tmp_path), AsyncMock()
        for _ in range(cfg.guard_layer.confirm_ticks):
            await glw.check_guard_layer_and_alert(cfg, disp)
        disp.reset_mock()
        monkeypatch.setattr(glw, "probe_guard_layer", AsyncMock(return_value=_healthy()))
        monkeypatch.setattr(glw, "probe_host_brain", AsyncMock(return_value=None))
        await glw.check_guard_layer_and_alert(cfg, disp)
        body = disp.send.call_args.args[0].body
        assert "not an all-clear" in body


class TestNotAGuardianHost:
    """`incus` absent is categorically different from `incus exec` failing.

    CI caught this and no local run could: removing the early-return so the host
    leg survives an unreachable container ALSO made the watch active on machines
    with no `incus` at all — a developer box, a CI runner — where it looked for the
    recovery brain, did not find it, and alerted. Two unrelated run_check tests
    started counting an extra alert.
    """

    @pytest.mark.asyncio
    async def test_no_incus_means_the_watch_is_inapplicable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(glw.shutil, "which", lambda name: None)
        probe = AsyncMock(return_value=_failing("venv_dead"))
        brain = AsyncMock(return_value=False)
        monkeypatch.setattr(glw, "probe_guard_layer", probe)
        monkeypatch.setattr(glw, "probe_host_brain", brain)
        cfg, disp = _Cfg(tmp_path), AsyncMock()
        for _ in range(cfg.guard_layer.confirm_ticks + 1):
            await glw.check_guard_layer_and_alert(cfg, disp)
        probe.assert_not_called()
        brain.assert_not_called()
        disp.send.assert_not_called()

    @pytest.mark.asyncio
    async def test_incus_present_but_exec_failing_STILL_runs_the_host_leg(
        self, tmp_path, monkeypatch
    ):
        """The distinction has to cut both ways, or the P1 fix is undone.

        A failing `incus exec` means the container is DOWN, which is exactly when
        the recovery brain matters most — so the host leg must still run there.
        """
        monkeypatch.setattr(glw.shutil, "which", lambda name: "/usr/bin/incus")
        monkeypatch.setattr(glw, "probe_guard_layer", AsyncMock(return_value=None))
        monkeypatch.setattr(glw, "probe_host_brain", AsyncMock(return_value=False))
        cfg, disp = _Cfg(tmp_path), AsyncMock()
        for _ in range(cfg.guard_layer.confirm_ticks):
            await glw.check_guard_layer_and_alert(cfg, disp)
        assert disp.send.call_count == 1
        assert "host_brain_dead" in disp.send.call_args.args[0].title


class TestRound4Regressions:
    def test_the_probe_runs_as_the_CONFIGURED_container_user(self, monkeypatch):
        """`container_user` is a supported setting the other collectors honour.

        Every path in the payload is $HOME-relative, so probing as the wrong user
        reports a healthy toolchain as broken, persistently, on any install that
        configures another user.
        """
        seen = {}

        async def _fake(container, cmd, stdin, timeout, user="ubuntu"):
            seen["user"] = user
            return 0, "GUARDLAYER ok"

        monkeypatch.setattr(glw, "_incus_exec_stdin", _fake)

        class _C(_Cfg):
            def __init__(self):
                super().__init__(Path("/tmp"))
                self.container_user = "someone-else"

        asyncio.run(glw.probe_guard_layer(_C()))
        assert seen["user"] == "someone-else", (
            "the probe hardcoded `ubuntu` instead of the configured container user"
        )

    def test_the_container_leg_probes_CC_not_just_node(self):
        """node being healthy is not evidence that Claude Code can start.

        The host leg makes exactly this argument for itself; the container leg was
        still inferring from a dependency, which is the same false-positive shape.
        """
        script = glw._PROBE_SCRIPT.decode()
        assert "claude --version" in script, (
            "the container leg infers CC startability from node, the dependency-proxy "
            "mistake the host leg explicitly avoids"
        )
        assert glw.CONDITION_CONTAINER_CC in script
        assert glw.CONDITION_CONTAINER_CC in glw._CONDITION_DETAIL

    def test_the_wrapper_no_longer_advertises_a_repair(self):
        """The contract in check.py must not promise behaviour that was removed."""
        src = Path(check_mod.__file__).read_text()
        start = src.index("async def _check_guard_layer_and_alert")
        doc = src[start:start + 600]
        assert "bounded repair" not in doc, (
            "the wrapper still describes a repair this watch does not perform"
        )

    def test_the_subprobe_budget_fits_inside_the_outer_timeout(self):
        """A per-subcheck bound that does not FIT is the same failure wearing a fix.

        An earlier version used 10s across six subchecks - 60s against a 30s outer
        default - so several wedged commands still consumed the whole budget before
        the marker printed, the probe parsed as None, and every condition went
        silent. This asserts the arithmetic rather than the number, so a seventh
        subcheck fails here instead of silently overcommitting.
        """
        script = glw._PROBE_SCRIPT.decode()
        spec = re.search(r'T="timeout -k (\d+) (\d+)"', script)
        assert spec, "the probe must bound each subcheck with an explicit kill-after"
        grace, per_check = int(spec.group(1)), int(spec.group(2))
        subchecks = script.count("$T ")
        worst_case = subchecks * (per_check + grace)
        outer = GuardLayerConfig().check_timeout_s
        assert worst_case < outer, (
            f"{subchecks} subchecks x ({per_check}s + {grace}s kill grace) = "
            f"{worst_case}s, which does not fit inside the {outer}s outer timeout. "
            f"Wedged commands would consume the budget before the marker prints, the "
            f"probe would parse as None, and every condition would go silent - the "
            f"exact failure the per-subcheck bound exists to prevent."
        )

    def test_the_subcheck_bound_sends_KILL_not_only_TERM(self):
        """Plain `timeout` sends TERM; a TERM-resistant command is then unbounded."""
        assert "timeout -k " in glw._PROBE_SCRIPT.decode()


# ── The SILENT class: every path that produces no signal must be DECLARED ─────
#
# Six of the thirteen findings on this module were the same defect wearing
# different clothes: A PATH THAT PRODUCES NO SIGNAL WHEN IT SHOULD PRODUCE ONE.
# Not a wrong alert — no alert, which for a detector is indistinguishable from
# health and is the worst failure available to it.
#
#   the host leg skipped when the container was unreachable
#   a persistent wedge that never escalated
#   a corrupt state file that wedged the watch forever
#   the watch queued behind a 3600s `claude -p`
#   one hung subcheck silencing every condition
#   a subprobe budget that did not fit inside the outer deadline
#
# Each was fixed as an instance. What made them POSSIBLE is that this module has
# many ways to exit without alerting — early returns, `continue`s, swallowed
# exceptions, probes that yield None — and NOTHING ENUMERATED THEM. Every one had
# to be spotted by a reviewer reading the code.
#
# So the inventory is explicit and asserted. Adding a silence path changes the
# count, which fails here with a message telling the author to justify it. That is
# the same allowlist polarity as the Bash-guard gate: a new path fails by
# construction rather than waiting for a seventh reviewer to notice it.
#
# This does NOT claim every listed silence is correct. It claims each one was
# written down on purpose, which is the part that was missing.
_DECLARED_SILENCE = {
    ("_parse_probe", "return-silence"): (
        1, "no GUARDLAYER marker means the probe is unreadable; an unreadable probe "
           "is NO SIGNAL, never a finding — git_watch's rule"),
    ("probe_guard_layer", "return-silence"): (
        2, "exec failure and a non-zero rc both mean the container is unreachable, "
           "which the confirmation state machine owns, not this watch"),
    ("probe_guard_layer", "except-swallow"): (
        1, "a timeout or OS error reaching the tick would take down every watch "
           "after it in run_check"),
    ("probe_host_brain", "return-silence"): (
        2, "a wedge is inconclusive rather than dead; the wedge STREAK is what "
           "escalates, so this silence is bounded by the caller"),
    ("probe_host_brain", "except-swallow"): (
        3, "the binary being absent, a timeout, and an unexpected error each reap "
           "the process group before returning, so none leaks a child"),
    ("check_guard_layer_and_alert", "return-silence"): (
        2, "the kill switch, and not being a guardian host at all — neither is a "
           "condition this watch can report on"),
    ("check_guard_layer_and_alert", "continue"): (
        2, "an INCONCLUSIVE condition (no evidence either way) and an episode that "
           "went healthy before ever alerting (nothing to resolve)"),
    ("check_guard_layer_and_alert", "except-swallow"): (
        1, "the tick-level swallow, which logs at WARNING precisely so this silence "
           "is visible in journald"),
    ("_load_state", "except-swallow"): (
        1, "unreadable or malformed state degrades to empty rather than wedging the "
           "watch — the defect that made every later tick repeat the same exception"),
    ("_save_state", "except-swallow"): (
        1, "failing to persist must not lose the alert that was already sent"),
    ("_send", "except-swallow"): (
        1, "a dead dispatcher must not abort the remaining conditions"),
}


def _silence_paths() -> dict:
    """Every (function, kind) that can exit without producing a signal."""
    tree = ast.parse(Path(glw.__file__).read_text(encoding="utf-8"))
    found: dict = {}
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for node in ast.walk(fn):
            kind = None
            if isinstance(node, ast.Return) and (
                node.value is None
                or (isinstance(node.value, ast.Constant) and node.value.value is None)
            ):
                kind = "return-silence"
            elif isinstance(node, ast.Continue):
                kind = "continue"
            elif isinstance(node, ast.ExceptHandler) and not any(
                isinstance(n, ast.Raise) for n in ast.walk(node)
            ):
                kind = "except-swallow"
            if kind:
                found[(fn.name, kind)] = found.get((fn.name, kind), 0) + 1
    return found


def test_every_silence_path_is_declared():
    """A new way to produce no signal must be written down, not discovered later."""
    found = _silence_paths()
    undeclared = sorted(set(found) - set(_DECLARED_SILENCE))
    assert not undeclared, (
        f"undeclared silence path(s): {undeclared}. This module can now exit without "
        f"producing a signal in a way nobody wrote down — six of the thirteen review "
        f"findings against it were exactly that. Add an entry to _DECLARED_SILENCE "
        f"stating WHY no alert is correct there, or make the path alert."
    )
    for key, count in sorted(found.items()):
        expected, reason = _DECLARED_SILENCE[key]
        assert count == expected, (
            f"{key} now has {count} silence paths, declared {expected} ({reason}). "
            f"A silence added to an already-justified function is still a silence "
            f"nobody justified."
        )


def test_the_silence_inventory_has_no_stale_entries():
    """Both directions: a declared path that no longer exists must leave the list."""
    found = _silence_paths()
    stale = sorted(set(_DECLARED_SILENCE) - set(found))
    assert not stale, (
        f"{stale} are declared but no longer present. An exemption for a path that "
        f"does not exist is debt, and it hides the next one that takes its place."
    )


class TestProbeVerifiesItsOwnTooling:
    """ENV class: "I cannot measure" and "the subject is broken" are different claims.

    MEASURED before the fix: with `timeout` absent from the container, every bounded
    subcheck fails and ALL SIX conditions report broken on a perfectly healthy
    toolchain. Six false alarms at once is how a watch earns being ignored, and it
    is the opposite polarity of the silent class the other findings were about.
    """

    def test_the_probe_checks_for_its_own_tooling_before_using_it(self):
        script = glw._PROBE_SCRIPT.decode()
        check_at = script.index("command -v timeout")
        first_use = script.index('T="timeout')
        assert check_at < first_use, (
            "the probe uses `timeout` before verifying it exists, so a container "
            "missing it reports every condition as broken on a healthy toolchain"
        )
        assert glw.CONDITION_PROBE_TOOLING in script

    @pytest.mark.asyncio
    async def test_unmeasurable_conditions_are_suppressed_not_reported_broken(
        self, tmp_path, monkeypatch
    ):
        """One honest condition, not six false ones."""
        monkeypatch.setattr(
            glw, "probe_guard_layer",
            AsyncMock(return_value={"failures": [glw.CONDITION_PROBE_TOOLING]}),
        )
        monkeypatch.setattr(glw, "probe_host_brain", AsyncMock(return_value=True))
        cfg, disp = _Cfg(tmp_path), AsyncMock()
        for _ in range(cfg.guard_layer.confirm_ticks):
            await glw.check_guard_layer_and_alert(cfg, disp)
        titles = [c.args[0].title for c in disp.send.call_args_list]
        assert len(titles) == 1, f"expected one alert, got {titles}"
        assert glw.CONDITION_PROBE_TOOLING in titles[0]

    @pytest.mark.asyncio
    async def test_unmeasurable_does_not_falsely_RESOLVE_a_warned_condition(
        self, tmp_path, monkeypatch
    ):
        """Suppressed must mean inconclusive, not healthy.

        Collapsing those two is the mistake that produced a false "recovered" on an
        inconclusive host probe; it must not be reintroduced through this path.
        """
        monkeypatch.setattr(glw, "probe_host_brain", AsyncMock(return_value=True))
        monkeypatch.setattr(glw, "probe_guard_layer", AsyncMock(return_value=_failing("venv_dead")))
        cfg, disp = _Cfg(tmp_path), AsyncMock()
        for _ in range(cfg.guard_layer.confirm_ticks):
            await glw.check_guard_layer_and_alert(cfg, disp)
        disp.reset_mock()
        monkeypatch.setattr(
            glw, "probe_guard_layer",
            AsyncMock(return_value={"failures": [glw.CONDITION_PROBE_TOOLING]}),
        )
        await glw.check_guard_layer_and_alert(cfg, disp)
        titles = [c.args[0].title for c in disp.send.call_args_list]
        assert not any("recovered: venv_dead" in t for t in titles), (
            "venv_dead was suppressed as unmeasurable and reported as RECOVERED - "
            "suppressed means no evidence, never good news"
        )
        assert "venv_dead" in glw._load_state(tmp_path / glw._STATE_FILE)
