"""Tests for the career-outreach monitor: config lever + obs-type governance.

The lever is an actuator gate (off/observe/live) that ships OFF; these tests lock
the degrade-toward-less-authority behavior and the architect's obs-governance
requirement (a new obs-type must be registered in BOTH governance sets).
"""

from __future__ import annotations

import json

import aiosqlite
import pytest
import pytest_asyncio

from genesis.db.crud import observations
from genesis.outreach.types import OutreachResult, OutreachStatus
from genesis.recon import career_outreach_config as cfg
from genesis.recon.career_outreach import (
    CareerOutreachMonitor,
    CareerOutreachResult,
    _nudge_hash,
)


def _write(tmp_path, text):
    p = tmp_path / "cfg.yaml"  # non-canonical stem → no real .local.yaml overlay
    p.write_text(text)
    return p


def test_ships_off_by_default(monkeypatch, tmp_path):
    # No config file at all → DEFAULTS → off (actuator inert on a fresh install).
    monkeypatch.delenv(cfg._ENV_KILL_SWITCH, raising=False)
    monkeypatch.setattr(cfg, "_base_path", lambda: tmp_path / "absent.yaml")
    assert cfg.effective_mode() == "off"


def test_default_config_ships_off():
    # The DEFAULTS themselves ship off (belt-and-suspenders on the shipped value).
    assert cfg.DEFAULTS["mode"] == "off"


def test_env_kill_switch_forces_off(monkeypatch, tmp_path):
    p = _write(tmp_path, "enabled: true\nmode: live\n")
    monkeypatch.setattr(cfg, "_base_path", lambda: p)
    monkeypatch.setenv(cfg._ENV_KILL_SWITCH, "1")
    assert cfg.effective_mode() == "off"


def test_enabled_false_forces_off(monkeypatch, tmp_path):
    p = _write(tmp_path, "enabled: false\nmode: live\n")
    monkeypatch.setattr(cfg, "_base_path", lambda: p)
    monkeypatch.delenv(cfg._ENV_KILL_SWITCH, raising=False)
    assert cfg.effective_mode() == "off"


def test_observe_and_live_pass_through(monkeypatch, tmp_path):
    monkeypatch.delenv(cfg._ENV_KILL_SWITCH, raising=False)
    for mode in ("observe", "live"):
        p = _write(tmp_path, f"enabled: true\nmode: {mode}\n")
        monkeypatch.setattr(cfg, "_base_path", lambda p=p: p)
        assert cfg.effective_mode() == mode


def test_invalid_mode_degrades_to_observe(monkeypatch, tmp_path):
    # Invalid → observe (seed/record safely; never a silent off that hides the
    # feature, never an unattended live).
    p = _write(tmp_path, "enabled: true\nmode: bogus\n")
    monkeypatch.setattr(cfg, "_base_path", lambda: p)
    monkeypatch.delenv(cfg._ENV_KILL_SWITCH, raising=False)
    assert cfg.effective_mode() == "observe"


def test_yaml_off_boolean_parses_as_off(monkeypatch, tmp_path):
    # Hand-edited unquoted `mode: off` → YAML-1.1 boolean False → off, not observe.
    p = _write(tmp_path, "enabled: true\nmode: off\n")
    monkeypatch.setattr(cfg, "_base_path", lambda: p)
    monkeypatch.delenv(cfg._ENV_KILL_SWITCH, raising=False)
    assert cfg.effective_mode() == "off"


def test_knob_int_damage_tolerant():
    d = cfg.DEFAULTS
    assert cfg.knob_int({"max_auto_runs_per_tick": 5}, "max_auto_runs_per_tick") == 5
    for bad in (0, -3, True, "x", None):
        assert (
            cfg.knob_int({"max_auto_runs_per_tick": bad}, "max_auto_runs_per_tick")
            == d["max_auto_runs_per_tick"]
        )
    assert cfg.knob_int({}, "dispatch_timeout_s") == d["dispatch_timeout_s"]


def test_knob_int_enforces_upper_bounds():
    # A typo must not authorize an unbounded run; dispatch_timeout_s must stay under
    # the 300s SSH cap.
    assert cfg.knob_int({"dispatch_timeout_s": 2400}, "dispatch_timeout_s") == 290
    assert cfg.knob_int({"max_auto_runs_per_tick": 3000}, "max_auto_runs_per_tick") == 10
    assert cfg.knob_int({"dispatch_timeout_s": 120}, "dispatch_timeout_s") == 120  # under cap


def test_module_name_damage_tolerant():
    d = cfg.DEFAULTS
    assert cfg.module_name({"reasoning_module": "My Ops"}, "reasoning_module") == "My Ops"
    assert cfg.module_name({"reasoning_module": "  "}, "reasoning_module") == d["reasoning_module"]
    assert cfg.module_name({"reasoning_module": 42}, "reasoning_module") == d["reasoning_module"]
    assert cfg.module_name({}, "reasoning_module") == d["reasoning_module"]


def test_dispatch_timeout_under_ssh_cap():
    # Must stay under the module's SSH CC hard cap (300s) so OUR timeout fires
    # cleanly rather than the adapter's opaque one.
    assert cfg.DEFAULTS["dispatch_timeout_s"] < 300


def test_nudge_obs_type_registered_in_both_governance_sets():
    # Architect concern #3: unregistered in INTERNAL_OBS_TYPES → leaks to user
    # surfacers; unregistered in _TTL_BY_TYPE → unknown-type warning every write.
    from genesis.db.crud.observations import _TTL_BY_TYPE, INTERNAL_OBS_TYPES

    assert "career_outreach_nudged" in INTERNAL_OBS_TYPES
    assert "career_outreach_nudged" in _TTL_BY_TYPE


# ── monitor tick tests (against a real in-memory observations store) ────────


@pytest_asyncio.fixture
async def db():
    from genesis.db.schema import create_all_tables

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    try:
        yield conn
    finally:
        await conn.close()


class _FakePipeline:
    def __init__(self, status: OutreachStatus = OutreachStatus.DELIVERED) -> None:
        self.sent: list = []
        self.status = status

    async def submit_raw(self, text, request):
        self.sent.append((text, request))
        return OutreachResult(
            outreach_id="fake", status=self.status, channel="telegram", message_content=text
        )


class _FakeModule:
    """Implements the real module contract the monitor drives: check_health_cached
    + execute_operation returning DICTS (the bridge never raises). Distinguishes
    the read-only list-working dispatch from an auto-run by the prompt text."""

    def __init__(
        self,
        *,
        healthy=True,
        autoruns=None,
        working=None,
        autorun_error=None,
        working_error=False,
        working_malformed=False,
        autorun_payload_error=None,
    ) -> None:
        self._healthy = healthy
        self._autoruns = list(autoruns or [])
        self._working = list(working or [])
        self._autorun_error = autorun_error  # adapter-level {"error"} for auto-run
        self._working_error = working_error  # adapter-level {"error"} for list
        self._working_malformed = working_malformed  # non-JSON-array list reply
        self._autorun_payload_error = autorun_payload_error  # module-reported {"error"}
        self.calls: list[str] = []

    async def check_health_cached(self) -> bool:
        return self._healthy

    async def execute_operation(self, op, params):
        prompt = (params or {}).get("prompt", "")
        self.calls.append(prompt)
        if "READ-ONLY: list" in prompt:
            if self._working_error:
                return {"error": "ssh down"}
            if self._working_malformed:
                return {"text": "sorry, I could not list them"}
            return {"text": json.dumps(self._working)}
        # auto-run dispatch
        if self._autorun_error is not None:
            return {"error": self._autorun_error}
        if self._autorun_payload_error is not None:
            return {"text": json.dumps({"error": self._autorun_payload_error})}
        if self._autoruns:
            return {"text": json.dumps(self._autoruns.pop(0))}
        return {"text": json.dumps({"none_left": True})}


class _FakeRegistry:
    def __init__(self, module):
        self._module = module

    def get(self, name):
        return self._module


def _autorun_calls(mod: _FakeModule) -> int:
    return sum(1 for c in mod.calls if "READ-ONLY: list" not in c)


def _cfg(monkeypatch, *, mode, cap=3):
    monkeypatch.setattr(cfg, "effective_mode", lambda: mode)
    monkeypatch.setattr(
        cfg,
        "load_config",
        lambda: {
            "reasoning_module": "test-module",
            "max_auto_runs_per_tick": cap,
            "dispatch_timeout_s": 240,
        },
    )


def _mon(db, module=None, *, pipe_status=OutreachStatus.DELIVERED, registry_none=False):
    mon = CareerOutreachMonitor(db)
    pipe = _FakePipeline(pipe_status)
    mon._pipeline = lambda: pipe
    mon._module_registry = (lambda: None) if registry_none else (lambda: _FakeRegistry(module))
    return mon, pipe


@pytest.mark.asyncio
async def test_off_mode_returns_immediately(db, monkeypatch):
    _cfg(monkeypatch, mode="off")
    mod = _FakeModule()
    mon, pipe = _mon(db, mod)
    result = await mon.gather()
    assert result.mode == "off"
    assert mod.calls == [] and pipe.sent == []


@pytest.mark.asyncio
async def test_absent_module_is_clean_noop_no_error(db, monkeypatch):
    # Architect concern #5: a generic install lacking the overlay module must
    # no-op cleanly — NOT a job-health failure (errors stays 0).
    _cfg(monkeypatch, mode="live")
    mon, pipe = _mon(db, None)  # registry.get() → None
    result = await mon.gather()
    assert result.mode == "live"
    assert result.errors == 0 and result.auto_runs == 0 and pipe.sent == []


@pytest.mark.asyncio
async def test_registry_unavailable_is_clean_noop(db, monkeypatch):
    _cfg(monkeypatch, mode="live")
    mon, pipe = _mon(db, _FakeModule(), registry_none=True)
    result = await mon.gather()
    assert result.mode == "live" and result.errors == 0 and pipe.sent == []


@pytest.mark.asyncio
async def test_unhealthy_module_clean_skip_no_dispatch(db, monkeypatch):
    # Architect concern #2: remote down → clean skip (health_ok False, errors 0),
    # and NO dispatch is attempted.
    _cfg(monkeypatch, mode="live")
    mod = _FakeModule(healthy=False)
    mon, pipe = _mon(db, mod)
    result = await mon.gather()
    assert result.health_ok is False and result.errors == 0
    assert mod.calls == [] and pipe.sent == []


@pytest.mark.asyncio
async def test_execute_operation_error_dict_surfaces_error(db, monkeypatch):
    # Architect concern #1: execute_operation returns an ERROR DICT (no raise) —
    # the tick must surface it as errors>0 (→ runner records job-health failure),
    # not silently succeed, and must not proceed to a second doomed dispatch.
    _cfg(monkeypatch, mode="live")
    mod = _FakeModule(autorun_error="SSH CC dispatch timed out after 240s")
    mon, pipe = _mon(db, mod)
    result = await mon.gather()
    assert result.errors >= 1 and result.auto_runs == 0
    assert _autorun_calls(mod) == 1  # broke the loop; did not keep dispatching
    assert pipe.sent == []  # adapter error → skip the nudge derivation


@pytest.mark.asyncio
async def test_n_zero_no_nudge(db, monkeypatch):
    # No target accounts and no staged drafts → NO "0 drafts" spam nudge.
    _cfg(monkeypatch, mode="live")
    mod = _FakeModule(autoruns=[], working=[])
    mon, pipe = _mon(db, mod)
    result = await mon.gather()
    assert result.nudged == 0 and pipe.sent == []


@pytest.mark.asyncio
async def test_observe_seeds_but_does_not_nudge(db, monkeypatch):
    _cfg(monkeypatch, mode="observe")
    mod = _FakeModule(working=[{"company": "Acme"}, {"company": "Globex"}])
    mon, pipe = _mon(db, mod)
    result = await mon.gather()
    assert result.mode == "observe" and result.seeded == 2
    assert pipe.sent == []  # observe never nudges
    # Both seeded into the dedup ledger.
    for co in ("Acme", "Globex"):
        assert await observations.exists_by_hash(db, source="recon", content_hash=_nudge_hash(co))


@pytest.mark.asyncio
async def test_live_nudges_only_new_drafts(db, monkeypatch):
    # Pre-seed one already-nudged account; live must nudge ONLY the new one.
    await observations.create(
        db,
        id="seed1",
        source="recon",
        type="career_outreach_nudged",
        content="seed:Acme",
        priority="low",
        created_at="2026-08-10T00:00:00Z",
        content_hash=_nudge_hash("Acme"),
        skip_if_duplicate=True,
    )
    _cfg(monkeypatch, mode="live")
    mod = _FakeModule(
        autoruns=[{"company": "Globex", "contact": "eng@globex.dev", "draft_summary": "x"}],
        working=[
            {"company": "Acme", "contact": "a@acme.dev"},
            {"company": "Globex", "contact": "eng@globex.dev"},
        ],
    )
    mon, pipe = _mon(db, mod)
    result = await mon.gather()
    assert result.auto_runs == 1 and result.nudged == 1
    assert len(pipe.sent) == 1
    body = pipe.sent[0][0]
    assert "Globex" in body and "Acme" not in body
    # Globex now marked; Acme still marked (unchanged).
    assert await observations.exists_by_hash(db, source="recon", content_hash=_nudge_hash("Globex"))


@pytest.mark.asyncio
async def test_cap_respected(db, monkeypatch):
    _cfg(monkeypatch, mode="live", cap=2)
    mod = _FakeModule(
        autoruns=[
            {"company": "A", "contact": "a", "draft_summary": "s"},
            {"company": "B", "contact": "b", "draft_summary": "s"},
            {"company": "C", "contact": "c", "draft_summary": "s"},  # would be a 3rd
        ],
        working=[],
    )
    mon, _ = _mon(db, mod)
    result = await mon.gather()
    assert result.auto_runs == 2
    assert _autorun_calls(mod) == 2  # stopped at the cap, never dispatched the 3rd


@pytest.mark.asyncio
async def test_undelivered_nudge_not_marked_retries(db, monkeypatch):
    # Undelivered nudge → account NOT marked, so it re-derives + retries next tick.
    _cfg(monkeypatch, mode="live")
    mod = _FakeModule(autoruns=[], working=[{"company": "Zeta", "contact": "z@zeta.dev"}])
    mon, pipe = _mon(db, mod, pipe_status=OutreachStatus.FAILED)
    result = await mon.gather()
    assert result.nudged == 0 and len(pipe.sent) == 1  # attempted, not delivered
    assert result.errors >= 1  # FAILED delivery counts as a job-health failure (B5)
    assert not await observations.exists_by_hash(
        db, source="recon", content_hash=_nudge_hash("Zeta")
    )


@pytest.mark.asyncio
async def test_nudge_gate_reopens_after_marker_resolved(db, monkeypatch):
    # unresolved_only=True: a nudged marker blocks re-nudge while unresolved; after
    # resolve_expired resolves it at TTL, the gate re-opens so a still-working draft
    # is gently re-nudged (bounded dedup, not permanent).
    _cfg(monkeypatch, mode="live")
    mod = _FakeModule(autoruns=[], working=[{"company": "Zeta", "contact": "z@zeta.dev"}])
    mon, pipe = _mon(db, mod)
    await observations.create(
        db,
        id="z1",
        source="recon",
        type="career_outreach_nudged",
        content="nudged:Zeta",
        priority="low",
        created_at="2026-08-10T00:00:00Z",
        content_hash=_nudge_hash("Zeta"),
        skip_if_duplicate=True,
    )
    r1 = await mon.gather()
    assert r1.nudged == 0 and pipe.sent == []  # blocked while unresolved
    await db.execute(
        "UPDATE observations SET resolved = 1 WHERE content_hash = ?", (_nudge_hash("Zeta"),)
    )
    await db.commit()
    r2 = await mon.gather()
    assert r2.nudged == 1 and len(pipe.sent) == 1  # gate re-opened → re-nudged


@pytest.mark.asyncio
async def test_health_check_raise_records_error(db, monkeypatch):
    # NOTE #5: a RAISED health check (vs clean False) surfaces errors>0 so a
    # persistently-throwing probe records a job-health FAILURE, not a silent skip.
    _cfg(monkeypatch, mode="live")

    class _RaisingModule(_FakeModule):
        async def check_health_cached(self):
            raise RuntimeError("probe boom")

    mon, pipe = _mon(db, _RaisingModule())
    result = await mon.gather()
    assert result.health_ok is False and result.errors == 1 and pipe.sent == []


@pytest.mark.asyncio
async def test_runner_records_failure_on_dispatch_errors(monkeypatch):
    # Architect concern #1 end-to-end: result.errors → runner records FAILURE.
    from genesis.surplus.jobs import runners

    calls = {"fail": [], "success": []}
    monkeypatch.setattr(runners, "record_failure", lambda k, m=None: calls["fail"].append((k, m)))
    monkeypatch.setattr(runners, "record_success", lambda k: calls["success"].append(k))

    class _StubMon:
        async def gather(self):
            return CareerOutreachResult(mode="live", errors=1, details=["adapter boom"])

    class _Sched:
        _career_outreach_monitor = _StubMon()
        _event_bus = None

    await runners.run_career_outreach_monitor(_Sched())
    assert calls["fail"] and calls["fail"][0][0] == "career_outreach_monitor"
    assert not calls["success"]


@pytest.mark.asyncio
async def test_runner_skips_health_record_when_off(monkeypatch):
    # Codex P2: mode="off" records NEITHER success nor failure — a disabled install
    # stays invisible in job-health (surplus convention).
    from genesis.surplus.jobs import runners

    calls = {"fail": [], "success": []}
    monkeypatch.setattr(runners, "record_failure", lambda k, m=None: calls["fail"].append(k))
    monkeypatch.setattr(runners, "record_success", lambda k: calls["success"].append(k))

    class _StubMon:
        async def gather(self):
            return CareerOutreachResult(mode="off")

    class _Sched:
        _career_outreach_monitor = _StubMon()
        _event_bus = None

    await runners.run_career_outreach_monitor(_Sched())
    assert not calls["fail"] and not calls["success"]


@pytest.mark.asyncio
async def test_pause_midtick_halts_dispatches_and_nudge(db, monkeypatch):
    # Codex P1: an owner /pause mid-tick stops the remaining dispatches AND the nudge.
    _cfg(monkeypatch, mode="live", cap=3)
    mod = _FakeModule(
        autoruns=[
            {"company": "A", "contact": "a", "draft_summary": "s"},
            {"company": "B", "contact": "b", "draft_summary": "s"},
        ],
        working=[{"company": "A", "contact": "a"}],
    )
    mon, pipe = _mon(db, mod)
    state = {"n": 0}

    def paused():
        state["n"] += 1
        return state["n"] > 1  # first check False (1 dispatch), then True

    mon._is_paused = paused
    result = await mon.gather()
    assert result.auto_runs == 1 and _autorun_calls(mod) == 1  # 2nd auto-run blocked
    assert result.nudged == 0 and pipe.sent == []  # nudge skipped while paused


@pytest.mark.asyncio
async def test_malformed_list_reply_is_error_observe_and_live(db, monkeypatch):
    # Codex P2: a NON-EMPTY, non-JSON-array list reply is a protocol violation →
    # errors≥1 (not a silent empty set that would make observe seed nothing, then
    # live blast the whole backlog).
    _cfg(monkeypatch, mode="observe")
    mon, _ = _mon(db, _FakeModule(working_malformed=True))
    r_obs = await mon.gather()
    assert r_obs.errors == 1 and r_obs.seeded == 0

    _cfg(monkeypatch, mode="live")
    mon2, pipe2 = _mon(db, _FakeModule(autoruns=[], working_malformed=True))
    r_live = await mon2.gather()
    assert r_live.errors >= 1 and pipe2.sent == []


@pytest.mark.asyncio
async def test_module_reported_error_counts_as_failure(db, monkeypatch):
    # Codex P2: a module-reported {"error"} in the auto-run payload increments errors
    # so a persistent module error surfaces as a job-health failure.
    _cfg(monkeypatch, mode="live")
    mon, _ = _mon(db, _FakeModule(autorun_payload_error="auth failed", working=[]))
    result = await mon.gather()
    assert result.errors >= 1


@pytest.mark.asyncio
async def test_ignored_nudge_is_not_an_error(db, monkeypatch):
    # Codex P2: a quiet-hours IGNORED nudge is an EXPECTED defer — NOT a failure —
    # and is not marked (retries next tick).
    _cfg(monkeypatch, mode="live")
    mod = _FakeModule(autoruns=[], working=[{"company": "Zeta", "contact": "z@zeta.dev"}])
    mon, pipe = _mon(db, mod, pipe_status=OutreachStatus.IGNORED)
    result = await mon.gather()
    assert result.errors == 0 and result.nudged == 0 and len(pipe.sent) == 1
    assert not await observations.exists_by_hash(
        db, source="recon", content_hash=_nudge_hash("Zeta")
    )


@pytest.mark.asyncio
async def test_missing_pipeline_nudge_counts_as_failure(db, monkeypatch):
    # Codex P2: no pipeline → the nudge cannot be delivered → a job-health failure.
    _cfg(monkeypatch, mode="live")
    mod = _FakeModule(autoruns=[], working=[{"company": "Zeta", "contact": "z@zeta.dev"}])
    mon, _ = _mon(db, mod)
    mon._pipeline = lambda: None
    result = await mon.gather()
    assert result.errors >= 1 and result.nudged == 0


@pytest.mark.asyncio
async def test_nudge_topic_distinguishes_distinct_sets(db):
    # Codex P2: distinct same-size same-day fresh sets get distinct topics (a stable
    # set-hash), so submit_raw's 24h (signal_type, topic, category) dedup does not
    # collapse two genuinely different batches into one.
    mon = CareerOutreachMonitor(db)
    sent = []

    class _CapturePipe:
        async def submit_raw(self, text, request):
            sent.append(request)
            return OutreachResult(
                outreach_id="x",
                status=OutreachStatus.DELIVERED,
                channel="telegram",
                message_content=text,
            )

    mon._pipeline = lambda: _CapturePipe()
    await mon._nudge([{"company": "Acme", "contact": ""}])
    await mon._nudge([{"company": "Globex", "contact": ""}])
    assert sent[0].topic != sent[1].topic


@pytest.mark.asyncio
async def test_runner_records_success_on_clean_tick(monkeypatch):
    from genesis.surplus.jobs import runners

    calls = {"fail": [], "success": []}
    monkeypatch.setattr(runners, "record_failure", lambda k, m=None: calls["fail"].append((k, m)))
    monkeypatch.setattr(runners, "record_success", lambda k: calls["success"].append(k))

    class _StubMon:
        async def gather(self):
            return CareerOutreachResult(mode="live", auto_runs=1, nudged=1)

    class _Sched:
        _career_outreach_monitor = _StubMon()
        _event_bus = None

    await runners.run_career_outreach_monitor(_Sched())
    assert calls["success"] == ["career_outreach_monitor"] and not calls["fail"]
