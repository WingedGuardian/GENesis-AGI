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
