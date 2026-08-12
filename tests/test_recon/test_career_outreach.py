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
    _AUTORUN_PROMPT,
    _LIST_WORKING_PROMPT,
    CareerOutreachMonitor,
    CareerOutreachResult,
    ModuleError,
    NoneLeft,
    ProtocolError,
    Staged,
    VerifyFailed,
    _autorun_prompt,
    _nudge_hash,
    _parse_json,
    classify_autorun_reply,
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
    # A typo must not authorize an unbounded run. dispatch_timeout_s is capped at
    # 1800s (the gated career-ops flow measured ~5.5 min; there is NO 300s SSH cap
    # — ipc.py::_send_cc takes a per-call timeout_s override with no ceiling).
    assert cfg.knob_int({"dispatch_timeout_s": 5000}, "dispatch_timeout_s") == 1800
    assert cfg.knob_int({"max_auto_runs_per_tick": 3000}, "max_auto_runs_per_tick") == 10
    assert cfg.knob_int({"dispatch_max_turns": 5000}, "dispatch_max_turns") == 200
    assert cfg.knob_int({"dispatch_timeout_s": 600}, "dispatch_timeout_s") == 600  # under cap
    assert cfg.knob_int({"dispatch_max_turns": 40}, "dispatch_max_turns") == 40  # under cap


def test_module_name_damage_tolerant():
    d = cfg.DEFAULTS
    assert cfg.module_name({"reasoning_module": "My Ops"}, "reasoning_module") == "My Ops"
    assert cfg.module_name({"reasoning_module": "  "}, "reasoning_module") == d["reasoning_module"]
    assert cfg.module_name({"reasoning_module": 42}, "reasoning_module") == d["reasoning_module"]
    assert cfg.module_name({}, "reasoning_module") == d["reasoning_module"]


def test_dispatch_timeout_default_covers_gated_flow():
    # The gated career-ops first-touch flow (research → draft → verify → stage)
    # measured ~5.5 min live; the default must comfortably cover it and stay within
    # its own configured cap. (There is NO 300s SSH cap — that was a false premise;
    # ipc.py::_send_cc honors a per-call timeout_s with no ceiling.)
    d = cfg.DEFAULTS["dispatch_timeout_s"]
    assert d == 900
    assert 0 < d <= cfg._MAX_BY_KNOB["dispatch_timeout_s"]


def test_dispatch_max_turns_default_and_cap():
    # The gated flow needed 42 turns live (> the ipc default of 25); the monitor
    # passes a generous max_turns so a cold-start (research+draft+verify+stage,
    # >42 turns) is not truncated. Damage-tolerant + capped.
    assert cfg.DEFAULTS["dispatch_max_turns"] == 80
    assert cfg.knob_int({}, "dispatch_max_turns") == 80
    assert 0 < cfg.DEFAULTS["dispatch_max_turns"] <= cfg._MAX_BY_KNOB["dispatch_max_turns"]


def test_text_knob_damage_tolerant():
    # autorun_note (overlay-only, appended to the auto-run prompt) is a str knob whose
    # DEFAULT is "" — blank/non-str degrade to "" (no note), a real value passes.
    assert cfg.text_knob({"autorun_note": "honor your gate"}, "autorun_note") == "honor your gate"
    assert cfg.text_knob({"autorun_note": "  "}, "autorun_note") == ""
    assert cfg.text_knob({"autorun_note": 42}, "autorun_note") == ""
    assert cfg.text_knob({}, "autorun_note") == ""


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
        working_empty_text=False,
        autorun_payload_error=None,
        autorun_malformed=False,
    ) -> None:
        self._healthy = healthy
        self._autoruns = list(autoruns or [])
        self._working = list(working or [])
        self._autorun_error = autorun_error  # adapter-level {"error"} for auto-run
        self._working_error = working_error  # adapter-level {"error"} for list
        self._working_malformed = working_malformed  # non-JSON-array list reply
        self._working_empty_text = working_empty_text  # empty-text list reply
        self._autorun_payload_error = autorun_payload_error  # module-reported {"error"}
        self._autorun_malformed = autorun_malformed  # unparseable auto-run reply
        self.calls: list[str] = []
        self.param_calls: list[dict] = []  # full params dict per dispatch (max_turns etc.)

    async def check_health_cached(self) -> bool:
        return self._healthy

    async def execute_operation(self, op, params):
        prompt = (params or {}).get("prompt", "")
        self.calls.append(prompt)
        self.param_calls.append(dict(params or {}))
        if "READ-ONLY: list" in prompt:
            if self._working_error:
                return {"error": "ssh down"}
            if self._working_malformed:
                return {"text": "sorry, I could not list them"}
            if self._working_empty_text:
                return {"text": ""}
            return {"text": json.dumps(self._working)}
        # auto-run dispatch
        if self._autorun_error is not None:
            return {"error": self._autorun_error}
        if self._autorun_payload_error is not None:
            return {"text": json.dumps({"error": self._autorun_payload_error})}
        if self._autorun_malformed:
            return {"text": "sorry, I could not do that"}
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


def _cfg(monkeypatch, *, mode, cap=3, autorun_note=""):
    monkeypatch.setattr(cfg, "effective_mode", lambda: mode)
    monkeypatch.setattr(
        cfg,
        "load_config",
        lambda: {
            "reasoning_module": "test-module",
            "max_auto_runs_per_tick": cap,
            "dispatch_timeout_s": 240,
            "dispatch_max_turns": 80,
            "autorun_note": autorun_note,
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
    # Codex r3 P1: the intervening list-staged read (another dispatch) is ALSO skipped.
    assert not any("READ-ONLY: list" in c for c in mod.calls)


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
async def test_rejected_nudge_is_not_an_error(db, monkeypatch):
    # Codex r3 P2: submit_raw skips quiet-hours governance so IGNORED never occurs; a
    # REJECTED nudge is pipeline dedup (the owner already got an identical one) — NOT a
    # failure, and not re-marked (a harmless re-derive next tick).
    _cfg(monkeypatch, mode="live")
    mod = _FakeModule(autoruns=[], working=[{"company": "Zeta", "contact": "z@zeta.dev"}])
    mon, pipe = _mon(db, mod, pipe_status=OutreachStatus.REJECTED)
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
async def test_unparseable_autorun_reply_counts_as_failure(db, monkeypatch):
    # Codex r3 P2: an unparseable auto-run reply is a protocol failure → errors≥1
    # (this also surfaces an injection-refusal reply as a job-health failure).
    _cfg(monkeypatch, mode="live")
    mon, _ = _mon(db, _FakeModule(autorun_malformed=True, working=[]))
    result = await mon.gather()
    assert result.errors >= 1 and result.auto_runs == 0


@pytest.mark.asyncio
async def test_autorun_missing_company_counts_as_failure(db, monkeypatch):
    # Codex r3 P2: an auto-run reply missing 'company' is a protocol failure → errors≥1.
    _cfg(monkeypatch, mode="live")
    mon, _ = _mon(db, _FakeModule(autoruns=[{"draft_summary": "x"}], working=[]))
    result = await mon.gather()
    assert result.errors >= 1 and result.auto_runs == 0


@pytest.mark.asyncio
async def test_empty_text_list_reply_is_error(db, monkeypatch):
    # Codex r3 P2: an empty-TEXT list reply (not "[]") is a protocol violation → error,
    # so observe never silently under-seeds (which would make live over-nudge).
    _cfg(monkeypatch, mode="observe")
    mon, _ = _mon(db, _FakeModule(working_empty_text=True))
    result = await mon.gather()
    assert result.errors == 1 and result.seeded == 0


@pytest.mark.asyncio
async def test_list_with_no_company_entries_is_error(db, monkeypatch):
    # Codex r3 P2: a non-empty list whose entries lack 'company' is malformed → error.
    _cfg(monkeypatch, mode="observe")
    mon, _ = _mon(db, _FakeModule(working=[{"draft_summary": "x"}]))
    result = await mon.gather()
    assert result.errors == 1 and result.seeded == 0


@pytest.mark.asyncio
async def test_valid_empty_array_list_is_not_error(db, monkeypatch):
    # A valid empty array "[]" = genuinely no staged drafts, NOT an error.
    _cfg(monkeypatch, mode="observe")
    mon, _ = _mon(db, _FakeModule(working=[]))
    result = await mon.gather()
    assert result.errors == 0 and result.seeded == 0


def test_enabled_string_false_degrades_to_off(monkeypatch, tmp_path):
    # Codex r3 P2: a string "false" (env-templated YAML) is truthy in Python — the
    # master switch requires a literal bool and degrades a non-bool to off.
    p = _write(tmp_path, 'enabled: "false"\nmode: live\n')
    monkeypatch.setattr(cfg, "_base_path", lambda: p)
    monkeypatch.delenv(cfg._ENV_KILL_SWITCH, raising=False)
    assert cfg.effective_mode() == "off"


def test_enabled_nonbool_int_degrades_to_off(monkeypatch, tmp_path):
    p = _write(tmp_path, "enabled: 1\nmode: live\n")  # truthy int, not literal True
    monkeypatch.setattr(cfg, "_base_path", lambda: p)
    monkeypatch.delenv(cfg._ENV_KILL_SWITCH, raising=False)
    assert cfg.effective_mode() == "off"


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


# ── C3-G: gate-compat auto-run fix (root cause = ground-truth-gate collision) ──


def test_autorun_prompt_honors_gate_and_headless():
    # The reworked auto-run prompt must (a) tell the module to run its OWN
    # verification gates (never skip/bypass), (b) declare the headless contract
    # (no interactive questions), (c) forbid gaming the gate (no fabricated token,
    # no stripping substance), (d) offer the verify_failed outcome, (e) never send.
    p = _AUTORUN_PROMPT.lower()
    assert "headless" in p
    assert "verify_failed" in _AUTORUN_PROMPT  # the new soft-failure outcome
    assert "never send" in p
    assert "fabricate" in p  # anti-gaming guard
    assert "reword" in p  # resolve attribution flags efficiently, don't loop
    assert "verification" in p or "verify" in p
    # still a GENERIC prompt — no engine-specific script/file names leak into the
    # shipped text (install-specific vocabulary rides the gitignored autorun_note).
    for ext in (".mjs", ".py", ".sh"):
        assert ext not in _AUTORUN_PROMPT
    # the read prompt is unchanged and does NOT trip the outreach/draft matcher class
    assert "READ-ONLY: list" in _LIST_WORKING_PROMPT


def test_autorun_note_appended_to_autorun_prompt_only():
    # The overlay note is appended to the AUTO-RUN prompt when set, and NOT when blank.
    base = _autorun_prompt({})
    assert base == _AUTORUN_PROMPT  # blank note → prompt unchanged
    noted = _autorun_prompt({"autorun_note": "ZZZ_INSTALL_NOTE"})
    assert noted.startswith(_AUTORUN_PROMPT) and "ZZZ_INSTALL_NOTE" in noted


@pytest.mark.asyncio
async def test_autorun_note_rides_autorun_dispatch_not_list(db, monkeypatch):
    # Red-team catch: the note must ride the auto-run dispatch but NOT the read
    # dispatch (a note with "outreach"/"ground-truth" would trip the consult matcher
    # on the currently-clean read path). Capture the actual dispatched prompts.
    _cfg(monkeypatch, mode="live", autorun_note="ZZZ_INSTALL_NOTE")
    mod = _FakeModule(autoruns=[], working=[{"company": "Z", "contact": "z@z.dev"}])
    mon, _ = _mon(db, mod)
    await mon.gather()
    autorun_prompts = [c for c in mod.calls if "READ-ONLY: list" not in c]
    list_prompts = [c for c in mod.calls if "READ-ONLY: list" in c]
    assert autorun_prompts and all("ZZZ_INSTALL_NOTE" in c for c in autorun_prompts)
    assert list_prompts and all("ZZZ_INSTALL_NOTE" not in c for c in list_prompts)


@pytest.mark.asyncio
async def test_verify_failed_is_not_error_and_continues(db, monkeypatch):
    # A verify_failed outcome = the gate correctly refused an unverifiable draft.
    # NOT a job-health error; the loop CONTINUES to try the next target.
    _cfg(monkeypatch, mode="live", cap=3)
    mod = _FakeModule(
        autoruns=[
            {"verify_failed": "dead source url", "company": "A"},
            {"company": "B", "contact": "b@b.dev", "draft_summary": "s"},
        ],
        working=[],
    )
    mon, _ = _mon(db, mod)
    result = await mon.gather()
    assert result.errors == 0  # verify_failed is not a failure
    assert result.auto_runs == 1  # only B staged; A correctly did not
    assert _autorun_calls(mod) == 3  # A(vf) → B(staged) → none_left


@pytest.mark.asyncio
async def test_verify_failed_repeat_guard_breaks(db, monkeypatch):
    # Same company returned twice (module lacks a durable "attempted" state) → break,
    # never an unbounded same-target loop burning the whole cap.
    _cfg(monkeypatch, mode="live", cap=3)
    mod = _FakeModule(
        autoruns=[
            {"verify_failed": "x", "company": "A"},
            {"verify_failed": "y", "company": "A"},  # re-picked A
        ],
        working=[],
    )
    mon, _ = _mon(db, mod)
    result = await mon.gather()
    assert result.errors == 0 and result.auto_runs == 0
    assert _autorun_calls(mod) == 2  # broke on the repeat, did not exhaust the cap


@pytest.mark.asyncio
async def test_staged_repeat_guard_breaks(db, monkeypatch):
    # The guard also covers the staged path: a re-picked already-staged company → break.
    _cfg(monkeypatch, mode="live", cap=3)
    mod = _FakeModule(
        autoruns=[
            {"company": "A", "contact": "a", "draft_summary": "s"},
            {"company": "A", "contact": "a", "draft_summary": "s"},  # re-picked A
        ],
        working=[],
    )
    mon, _ = _mon(db, mod)
    result = await mon.gather()
    assert result.auto_runs == 1 and _autorun_calls(mod) == 2


@pytest.mark.asyncio
async def test_autorun_dispatch_passes_max_turns_list_does_not(db, monkeypatch):
    # The auto-run dispatch carries max_turns (the gated flow needs >25 turns);
    # the lightweight read dispatch does not (keeps the ipc default).
    _cfg(monkeypatch, mode="live")
    mod = _FakeModule(autoruns=[], working=[{"company": "Z", "contact": "z@z.dev"}])
    mon, _ = _mon(db, mod)
    await mon.gather()
    paired = list(zip(mod.param_calls, mod.calls, strict=True))
    autorun_params = [p for p, c in paired if "READ-ONLY: list" not in c]
    list_params = [p for p, c in paired if "READ-ONLY: list" in c]
    assert autorun_params and all(p.get("max_turns") == 80 for p in autorun_params)
    assert list_params and all("max_turns" not in p for p in list_params)


@pytest.mark.asyncio
async def test_verify_failed_blank_reason_not_counted_as_staged(db, monkeypatch):
    # Review NOTE 2: an empty-string verify_failed reason must be treated as
    # verify_failed (presence-check), NOT fall through to the staged path and
    # miscount a draft that never staged.
    _cfg(monkeypatch, mode="live", cap=3)
    mod = _FakeModule(autoruns=[{"verify_failed": "", "company": "A"}], working=[])
    mon, _ = _mon(db, mod)
    result = await mon.gather()
    assert result.auto_runs == 0 and result.verify_failed == 1 and result.errors == 0


@pytest.mark.asyncio
async def test_contradictory_multi_signal_reply_is_error(db, monkeypatch):
    # Class audit: two+ outcome signals in one reply (none_left / error / verify_failed)
    # is a contradictory malformed reply → protocol error, never a silent swallow where
    # the first-checked signal wins (e.g. none_left + verify_failed).
    for combo in (
        {"none_left": True, "verify_failed": "v", "company": "A"},
        {"error": "e", "verify_failed": "v", "company": "A"},
        {"none_left": True, "error": "e"},
    ):
        _cfg(monkeypatch, mode="live", cap=3)
        mon, _ = _mon(db, _FakeModule(autoruns=[combo], working=[]))
        result = await mon.gather()
        assert result.errors >= 1 and result.auto_runs == 0 and result.verify_failed == 0


@pytest.mark.asyncio
async def test_empty_dict_reply_is_error(db, monkeypatch):
    # An empty dict reply is a protocol violation (no outcome) → error, not swallowed.
    _cfg(monkeypatch, mode="live", cap=3)
    mon, _ = _mon(db, _FakeModule(autoruns=[{}], working=[]))
    result = await mon.gather()
    assert result.errors >= 1 and result.auto_runs == 0


def test_parse_json_never_raises_on_deep_nesting():
    # Class audit NOTE 2: a pathologically-nested reply must NOT propagate a
    # RecursionError out of the best-effort parser (it would be miscounted as a
    # job-health failure); it degrades to None/best-effort instead.
    deep = "[" * 2000 + "]" * 2000  # > the default recursion limit
    out = _parse_json(deep)  # must not raise
    assert out is None or isinstance(out, list)


@pytest.mark.asyncio
async def test_verify_failed_missing_company_is_error(db, monkeypatch):
    # Codex P2: a verify_failed reply with NO company is a protocol violation → error
    # (parity with the missing-company staged path), not a silent successful tick.
    _cfg(monkeypatch, mode="live", cap=3)
    mod = _FakeModule(autoruns=[{"verify_failed": "x"}], working=[])
    mon, _ = _mon(db, mod)
    result = await mon.gather()
    assert result.errors >= 1 and result.auto_runs == 0 and result.verify_failed == 0


@pytest.mark.asyncio
async def test_runner_warns_but_succeeds_on_no_progress_verify_failed(monkeypatch):
    # Review SHOULD-FIX 1: a verify_failed-only tick (0 staged/nudged) records SUCCESS
    # (the gate working, not a failure) BUT emits a WARNING event so a persistently
    # self-refusing engine is not invisible to job-health.
    from genesis.observability.types import Severity
    from genesis.surplus.jobs import runners

    calls = {"fail": [], "success": []}
    monkeypatch.setattr(runners, "record_failure", lambda k, m=None: calls["fail"].append(k))
    monkeypatch.setattr(runners, "record_success", lambda k: calls["success"].append(k))
    events: list = []

    class _Bus:
        async def emit(self, subsystem, severity, code, msg, **kw):
            events.append((severity, code))

    class _StubMon:
        async def gather(self):
            return CareerOutreachResult(mode="live", verify_failed=2)

    class _Sched:
        _career_outreach_monitor = _StubMon()
        _event_bus = _Bus()

    await runners.run_career_outreach_monitor(_Sched())
    assert calls["success"] == ["career_outreach_monitor"] and not calls["fail"]
    assert any(
        sev == Severity.WARNING and code == "career_outreach.no_progress" for sev, code in events
    )


def test_parse_json_extracts_dict_from_verdict_led_reply():
    # The engine may lead with its own verdict line, THEN the JSON — the parser must
    # extract the outermost {..} even past a verdict line containing [..] brackets.
    reply = (
        "VERDICT: pass — 3 sources verified, claims [role, team], confirmed n/a\n"
        '{"company":"Acme AI","contact":"founder@acme.example","draft_summary":"cold email"}'
    )
    out = _parse_json(reply)
    assert isinstance(out, dict) and out["company"] == "Acme AI"


# ── classify_autorun_reply: the closed-outcome contract (the ONE locking table) ──


@pytest.mark.parametrize(
    "reply, expected",
    [
        # NoneLeft
        ('{"none_left": true}', NoneLeft()),
        # Staged (contact/draft_summary optional → "")
        (
            '{"company":"Acme","contact":"c@a.example","draft_summary":"s"}',
            Staged("Acme", "c@a.example", "s"),
        ),
        ('{"company":"Acme"}', Staged("Acme", "", "")),
        # verdict-line-led + fenced still classify (parse layer)
        (
            'GT: pass — claims [a, b]\n{"company":"Acme","contact":"c","draft_summary":"s"}',
            Staged("Acme", "c", "s"),
        ),
        ('```json\n{"none_left": true}\n```', NoneLeft()),
        # VerifyFailed (blank/null reason → "unspecified")
        ('{"verify_failed":"dead url","company":"Acme"}', VerifyFailed("Acme", "dead url")),
        ('{"verify_failed":"","company":"Acme"}', VerifyFailed("Acme", "unspecified")),
        ('{"verify_failed":null,"company":"Acme"}', VerifyFailed("Acme", "unspecified")),
        ('{"verify_failed":123,"company":"Acme"}', VerifyFailed("Acme", "123")),  # non-str reason
        # ModuleError
        ('{"error":"auth failed"}', ModuleError("auth failed")),
        # verdict line + FENCED json (a plausible single reply) still classifies
        (
            'GT: pass\n```json\n{"company":"Acme","contact":"c","draft_summary":"s"}\n```',
            Staged("Acme", "c", "s"),
        ),
    ],
)
def test_classify_autorun_reply_valid_outcomes(reply, expected):
    assert classify_autorun_reply(reply) == expected


@pytest.mark.parametrize(
    "reply",
    [
        "not json at all",
        "",
        "   ",
        "[1, 2, 3]",  # array, not an object
        "42",  # scalar
        '"a string"',  # scalar
        "null",  # json null
        "{}",  # empty object — no outcome
        '{"none_left": false}',  # present but not literal true
        '{"none_left": "yes"}',  # wrong type
        '{"none_left": 1}',  # numeric-truthy, not literal true
        '{"error": ""}',  # empty error detail
        '{"error": null}',  # null error detail
        '{"error": 123}',  # non-string error
        '{"verify_failed":"x"}',  # verify_failed missing company
        '{"verify_failed":"x","company":"   "}',  # whitespace company
        '{"contact":"c","draft_summary":"s"}',  # no company, no signal
        '{"company": 123}',  # company wrong type
        '{"none_left":true,"verify_failed":"x","company":"A"}',  # multi-signal
        '{"error":"e","verify_failed":"v","company":"A"}',  # multi-signal
        '{"none_left":true,"error":"e"}',  # multi-signal
    ],
)
def test_classify_autorun_reply_protocol_errors(reply):
    assert isinstance(classify_autorun_reply(reply), ProtocolError)


def test_parse_json_tolerates_trailing_content():
    # Review SHOULD-FIX: trailing content after the payload (a closing ``` fence, a
    # trailing word/period, or a leading verdict line PLUS a fence) must still recover
    # the object — the strict consumes-to-end scan falls back to the outermost span.
    assert _parse_json('{"company":"X"}\nDone.')["company"] == "X"
    assert _parse_json('{"company":"X"}.')["company"] == "X"
    assert _parse_json('GT: pass\n```json\n{"company":"X"}\n```')["company"] == "X"
    # ...while the leading verdict-is-JSON case still resolves to the TRAILING payload.
    assert _parse_json('{"verdict":"pass"}\n{"company":"X"}')["company"] == "X"
    # ...and a nested object with trailing content recovers the top-level object.
    assert _parse_json('{"company":"Y","meta":{"a":1}} trailing')["company"] == "Y"


def test_parse_json_handles_json_verdict_and_nested_objects():
    # Codex P2: a verdict line that is ITSELF JSON must not be mistaken for the payload
    # (first-{ span bug), and a nested object must not be grabbed (last-{ span bug).
    # The trailing payload = the value that consumes to the end of the reply.
    assert _parse_json('{"verdict":"pass"}\n{"company":"X","contact":"c"}')["company"] == "X"
    nested = _parse_json('note line\n{"company":"Y","meta":{"a":1}}')
    assert nested["company"] == "Y" and nested["meta"] == {"a": 1}
    # a clean array (read path, no verdict) still parses
    assert _parse_json('[{"company":"A"},{"company":"B"}]') == [
        {"company": "A"},
        {"company": "B"},
    ]
