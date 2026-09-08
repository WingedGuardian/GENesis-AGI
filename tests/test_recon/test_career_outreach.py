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
    _PROBE_PROMPT,
    BITE_STAGES,
    CareerOutreachMonitor,
    CareerOutreachResult,
    ModuleError,
    NoneLeft,
    ProtocolError,
    Staged,
    VerifyFailed,
    _autorun_prompt,
    _bite_hash,
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


# ── C: bite-relay config lever (independent of the auto-run mode) ────────────


def test_bite_relay_ships_off_and_data_module_empty():
    # The bite-relay ships OFF + empty data_module → a generic install skips it,
    # and no install-specific module name lands in the public repo.
    assert cfg.DEFAULTS["bite_relay_mode"] == "off"
    assert cfg.DEFAULTS["data_module"] == ""


def test_effective_bite_relay_mode_default_off(monkeypatch, tmp_path):
    # No config file → DEFAULTS → off (opt-in actuator inert on a fresh install).
    monkeypatch.delenv(cfg._ENV_KILL_SWITCH, raising=False)
    monkeypatch.setattr(cfg, "_base_path", lambda: tmp_path / "absent.yaml")
    assert cfg.effective_bite_relay_mode() == "off"


def test_effective_bite_relay_mode_pass_through(monkeypatch, tmp_path):
    monkeypatch.delenv(cfg._ENV_KILL_SWITCH, raising=False)
    for m in ("observe", "live"):
        p = _write(tmp_path, f"enabled: true\nbite_relay_mode: {m}\n")
        monkeypatch.setattr(cfg, "_base_path", lambda p=p: p)
        assert cfg.effective_bite_relay_mode() == m


def test_effective_bite_relay_mode_invalid_fails_closed_to_off(monkeypatch, tmp_path):
    # Invalid bite_relay_mode → OFF (NOT observe): the relay's observe SEEDS permanent
    # markers, so degrading a typo to observe would silently mark the current backlog as
    # relayed and suppress it forever once corrected to live. Fail closed for the seeding
    # lever. (Contrast the auto-run's harmless observe degrade, locked just below.)
    p = _write(tmp_path, "enabled: true\nbite_relay_mode: bogus\n")
    monkeypatch.setattr(cfg, "_base_path", lambda: p)
    monkeypatch.delenv(cfg._ENV_KILL_SWITCH, raising=False)
    assert cfg.effective_bite_relay_mode() == "off"


def test_effective_mode_invalid_still_degrades_to_observe(monkeypatch, tmp_path):
    # The AUTO-RUN's invalid-mode degrade is UNCHANGED — observe is a harmless bridge
    # reachability probe (stages/seeds nothing), so only the SEEDING bite-relay diverges
    # to off. Locks that the two levers degrade differently on purpose.
    p = _write(tmp_path, "enabled: true\nmode: bogus\n")
    monkeypatch.setattr(cfg, "_base_path", lambda: p)
    monkeypatch.delenv(cfg._ENV_KILL_SWITCH, raising=False)
    assert cfg.effective_mode() == "observe"


def test_effective_bite_relay_mode_kill_switch_and_enabled_false(monkeypatch, tmp_path):
    # The whole-monitor kill switch + master enabled flag force the bite-relay off too.
    p = _write(tmp_path, "enabled: true\nbite_relay_mode: live\n")
    monkeypatch.setattr(cfg, "_base_path", lambda: p)
    monkeypatch.setenv(cfg._ENV_KILL_SWITCH, "1")
    assert cfg.effective_bite_relay_mode() == "off"
    monkeypatch.delenv(cfg._ENV_KILL_SWITCH, raising=False)
    p2 = _write(tmp_path, "enabled: false\nbite_relay_mode: live\n")
    monkeypatch.setattr(cfg, "_base_path", lambda: p2)
    assert cfg.effective_bite_relay_mode() == "off"


def test_bite_relay_lever_independent_of_auto_run(monkeypatch, tmp_path):
    # The two levers are INDEPENDENT: auto-run off, bite-relay live (the whole point —
    # the relay can go live while the policy-blocked auto-run stays gated).
    p = _write(tmp_path, "enabled: true\nmode: off\nbite_relay_mode: live\n")
    monkeypatch.setattr(cfg, "_base_path", lambda: p)
    monkeypatch.delenv(cfg._ENV_KILL_SWITCH, raising=False)
    assert cfg.effective_mode() == "off"
    assert cfg.effective_bite_relay_mode() == "live"


def test_career_bite_obs_type_registered_in_both_governance_sets():
    # A stage-advance bite obs must register in BOTH sets (same governance requirement
    # as career_outreach_nudged) — INTERNAL_OBS_TYPES (no user-surfacer leak) + _TTL_BY_TYPE.
    from genesis.db.crud.observations import _TTL_BY_TYPE, INTERNAL_OBS_TYPES

    assert "career_bite" in INTERNAL_OBS_TYPES
    assert "career_bite" in _TTL_BY_TYPE


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
    + execute_operation returning DICTS (the bridge never raises). Distinguishes the
    observe-mode reachability PROBE from an auto-run dispatch by the prompt text."""

    def __init__(
        self,
        *,
        healthy=True,
        autoruns=None,
        autorun_error=None,
        autorun_payload_error=None,
        autorun_malformed=False,
        probe_error=False,
        probe_empty=False,
        probe_is_error=False,
    ) -> None:
        self._healthy = healthy
        self._autoruns = list(autoruns or [])
        self._autorun_error = autorun_error  # adapter-level {"error"} for auto-run
        self._autorun_payload_error = autorun_payload_error  # module-reported {"error"}
        self._autorun_malformed = autorun_malformed  # unparseable auto-run reply
        self._probe_error = probe_error  # adapter-level {"error"} for the observe probe
        self._probe_empty = probe_empty  # empty-text probe reply
        self._probe_is_error = probe_is_error  # payload {"is_error": True} (dead-auth, exit 0)
        self.calls: list[str] = []
        self.param_calls: list[dict] = []  # full params dict per dispatch (max_turns etc.)

    async def check_health_cached(self) -> bool:
        return self._healthy

    async def execute_operation(self, op, params):
        prompt = (params or {}).get("prompt", "")
        self.calls.append(prompt)
        self.param_calls.append(dict(params or {}))
        if prompt == _PROBE_PROMPT:  # observe-mode reachability probe
            if self._probe_error:
                return {"error": "ssh down"}
            if self._probe_is_error:
                return {"text": "Invalid API key / OAuth expired", "is_error": True}
            if self._probe_empty:
                return {"text": ""}
            return {"text": '{"ok":true}'}
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
    # Auto-run dispatches = everything except the observe-mode reachability probe.
    return sum(1 for c in mod.calls if c != _PROBE_PROMPT)


def _cfg(
    monkeypatch,
    *,
    mode,
    bite_mode="off",
    cap=3,
    bite_cap=10,
    autorun_note="",
    data_module="data-module",
):
    monkeypatch.setattr(cfg, "effective_mode", lambda: mode)
    # Stub the INDEPENDENT bite-relay lever too (default off) so the existing auto-run
    # tests never spring the relay to observe via the invalid→observe degrade.
    monkeypatch.setattr(cfg, "effective_bite_relay_mode", lambda: bite_mode)
    monkeypatch.setattr(
        cfg,
        "load_config",
        lambda: {
            "reasoning_module": "test-module",
            "data_module": data_module,
            "max_auto_runs_per_tick": cap,
            "max_bite_nudges_per_tick": bite_cap,
            "dispatch_timeout_s": 240,
            "dispatch_max_turns": 80,
            "autorun_note": autorun_note,
        },
    )


# Stage list the external /api/pipeline returns (verified from jerbs pipeline_api.py).
_PIPE_STAGES = (
    "researching",
    "contacts_mapped",
    "outreach_sent",
    "in_conversation",
    "interviewing",
    "offer",
    "closed",
)


class _FakeDataModule:
    """The HTTP data module the bite-relay reads: check_health_cached +
    execute_operation('pipeline') returning {stages, pipeline: {stage: [entries]}}."""

    def __init__(
        self,
        *,
        healthy=True,
        pipeline=None,
        error=None,
        bad_shape=False,
        raise_health=False,
        raise_op=False,
    ) -> None:
        self._healthy = healthy
        self._pipeline = pipeline or {}
        self._error = error
        self._bad_shape = bad_shape
        self._raise_health = raise_health
        self._raise_op = raise_op
        self.ops: list[str] = []

    async def check_health_cached(self) -> bool:
        if self._raise_health:
            raise RuntimeError("data health boom")
        return self._healthy

    async def execute_operation(self, op, params=None):
        self.ops.append(op)
        if self._raise_op:
            raise ValueError("malformed JSON body")  # e.g. resp.json() raising in ipc
        if self._error is not None:
            return {"error": self._error}
        if self._bad_shape:
            return {"stages": list(_PIPE_STAGES), "pipeline": "not-a-dict"}
        return {"stages": list(_PIPE_STAGES), "pipeline": self._pipeline}


class _FakeMultiRegistry:
    """Resolves DIFFERENT modules by name — the bite-relay reads 'data-module', the
    auto-run drives 'test-module' (the reasoning module)."""

    def __init__(self, modules: dict):
        self._modules = modules

    def get(self, name):
        return self._modules.get(name)


def _entry(cid, name, tier="A"):
    return {"id": cid, "name": name, "priority_tier": tier, "industry": ""}


def _mon_bite(db, data_module, *, reasoning_module=None, pipe_status=OutreachStatus.DELIVERED):
    mon = CareerOutreachMonitor(db)
    pipe = _FakePipeline(pipe_status)
    mon._pipeline = lambda: pipe
    mon._module_registry = lambda: _FakeMultiRegistry(
        {"data-module": data_module, "test-module": reasoning_module}
    )
    return mon, pipe


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
async def test_registry_unavailable_with_enabled_lever_is_failure(db, monkeypatch):
    # Codex #1590: an enabled lever + a None module REGISTRY (a Genesis-side wiring failure,
    # distinct from the remote's per-module health being down) is silently dead → a
    # job-health FAILURE (errors=1), not a green no-op. Reached only past the both-off return.
    _cfg(monkeypatch, mode="live")
    mon, pipe = _mon(db, _FakeModule(), registry_none=True)
    result = await mon.gather()
    assert result.mode == "live" and result.errors == 1 and pipe.sent == []


@pytest.mark.asyncio
async def test_unhealthy_module_records_failure_no_dispatch(db, monkeypatch):
    # An unreachable bridge (check_health_cached False) is a job-health FAILURE, not a
    # clean skip: observe exists to detect a dead bridge, so a persistent outage must
    # SURFACE (errors=1 → record_failure → PR #1428 / gap detector). No dispatch is
    # attempted. mode=live proves it is mode-independent (also fixes the earlier bug
    # where errors=0 booked a false success that masked the never-succeeded alarm).
    _cfg(monkeypatch, mode="live")
    mod = _FakeModule(healthy=False)
    mon, pipe = _mon(db, mod)
    result = await mon.gather()
    assert result.health_ok is False and result.errors == 1
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
    mod = _FakeModule(autoruns=[])
    mon, pipe = _mon(db, mod)
    result = await mon.gather()
    assert result.nudged == 0 and pipe.sent == []


@pytest.mark.asyncio
async def test_observe_probe_reachable_records_success_no_stage_no_nudge(db, monkeypatch):
    # Observe = a bridge-reachability PROBE, not a seeder. A reachable bridge → errors=0
    # (runner records job-health SUCCESS, clearing a never-succeeded alarm once alive);
    # stages nothing, nudges nothing, writes NOTHING to the observations store.
    _cfg(monkeypatch, mode="observe")
    mod = _FakeModule()
    mon, pipe = _mon(db, mod)
    result = await mon.gather()
    assert result.mode == "observe" and result.errors == 0
    assert pipe.sent == []  # observe never nudges
    assert mod.calls == [_PROBE_PROMPT]  # exactly one dispatch — the probe
    rows = await (await db.execute("SELECT COUNT(*) FROM observations")).fetchone()
    assert rows[0] == 0  # nothing seeded


@pytest.mark.asyncio
async def test_observe_probe_adapter_error_records_failure(db, monkeypatch):
    # A dead bridge (adapter {"error"}) → errors=1 so the daily observe tick records a
    # job-health FAILURE instead of failing silently (the 8-day-outage class).
    _cfg(monkeypatch, mode="observe")
    mon, pipe = _mon(db, _FakeModule(probe_error=True))
    result = await mon.gather()
    assert result.mode == "observe" and result.errors == 1
    assert pipe.sent == []


@pytest.mark.asyncio
async def test_observe_probe_is_error_payload_records_failure(db, monkeypatch):
    # A dead-model/OAuth `claude -p` can exit 0 with {"is_error": true, "text": "<auth
    # error>"} and NO top-level "error" key. The probe must treat is_error as a FAILURE —
    # else it records a dead bridge as reachable, the exact silent failure it exists to
    # catch (review finding 1).
    _cfg(monkeypatch, mode="observe")
    mon, pipe = _mon(db, _FakeModule(probe_is_error=True))
    result = await mon.gather()
    assert result.mode == "observe" and result.errors == 1
    assert pipe.sent == []


@pytest.mark.asyncio
async def test_observe_probe_empty_reply_records_failure(db, monkeypatch):
    # An empty-text reply is not proof of reachability (the OAuth hop may have half-failed)
    # → errors=1. A non-empty reply of ANY content is success (the reachable test above).
    _cfg(monkeypatch, mode="observe")
    mon, _ = _mon(db, _FakeModule(probe_empty=True))
    result = await mon.gather()
    assert result.errors == 1


@pytest.mark.asyncio
async def test_observe_probe_prompt_is_hook_safe(db, monkeypatch):
    # The probe prompt carries NO outreach/draft/stage vocabulary (which would trip the
    # external engine's own outreach hooks on the otherwise-clean observe path); observe
    # dispatches EXACTLY that prompt with a small bounded turn budget.
    for word in ("outreach", "draft", "stage"):
        assert word not in _PROBE_PROMPT.lower()
    _cfg(monkeypatch, mode="observe")
    mod = _FakeModule()
    mon, _ = _mon(db, mod)
    await mon.gather()
    assert mod.calls == [_PROBE_PROMPT]
    assert mod.param_calls[0].get("max_turns") == 2  # _PROBE_MAX_TURNS, not the 80 knob


@pytest.mark.asyncio
async def test_live_nudges_only_fresh_staged_drafts(db, monkeypatch):
    # The nudge derives from THIS tick's staged drafts, deduped against the ledger — no
    # census. Pre-mark Acme; stage both Acme and Globex this tick → nudge ONLY Globex.
    await observations.create(
        db,
        id="seed1",
        source="recon",
        type="career_outreach_nudged",
        content="nudged:Acme",
        priority="low",
        created_at="2026-08-10T00:00:00Z",
        content_hash=_nudge_hash("Acme"),
        skip_if_duplicate=True,
    )
    _cfg(monkeypatch, mode="live")
    mod = _FakeModule(
        autoruns=[
            {"company": "Acme", "contact": "a@acme.dev", "draft_summary": "x"},
            {"company": "Globex", "contact": "eng@globex.dev", "draft_summary": "x"},
        ],
    )
    mon, pipe = _mon(db, mod)
    result = await mon.gather()
    assert result.auto_runs == 2  # both staged this tick
    assert result.nudged == 1  # but only the un-nudged one is surfaced
    assert len(pipe.sent) == 1
    body = pipe.sent[0][0]
    assert "Globex" in body and "Acme" not in body
    assert await observations.exists_by_hash(db, source="recon", content_hash=_nudge_hash("Globex"))


@pytest.mark.asyncio
async def test_stage_then_adapter_error_still_nudges_staged(db, monkeypatch):
    # Architect SHOULD-FIX #1: if the loop stages A then dispatch #2 hits an adapter
    # error, the nudge for A must STILL go out (the engine won't re-stage A, so skipping
    # would strand it) — and the adapter error is still counted as a job-health failure.
    _cfg(monkeypatch, mode="live", cap=3)

    class _StageThenError(_FakeModule):
        async def execute_operation(self, op, params):
            prompt = (params or {}).get("prompt", "")
            self.calls.append(prompt)
            self.param_calls.append(dict(params or {}))
            if len(self.calls) == 1:  # first auto-run stages A
                return {
                    "text": json.dumps({"company": "A", "contact": "a@a.dev", "draft_summary": "s"})
                }
            return {"error": "SSH CC dispatch timed out"}  # second dispatch errors

    mod = _StageThenError()
    mon, pipe = _mon(db, mod)
    result = await mon.gather()
    assert result.auto_runs == 1 and result.errors >= 1  # A staged; the timeout counted
    assert result.nudged == 1 and len(pipe.sent) == 1  # A still nudged despite the error
    assert "A" in pipe.sent[0][0]
    assert await observations.exists_by_hash(db, source="recon", content_hash=_nudge_hash("A"))


@pytest.mark.asyncio
async def test_drafts_working_equals_auto_runs(db, monkeypatch):
    # drafts_working now counts exactly the drafts staged this tick, so it always equals
    # auto_runs (the census that once made them diverge is gone).
    _cfg(monkeypatch, mode="live", cap=3)
    mod = _FakeModule(
        autoruns=[
            {"company": "A", "contact": "a", "draft_summary": "s"},
            {"company": "B", "contact": "b", "draft_summary": "s"},
        ],
    )
    mon, _ = _mon(db, mod)
    result = await mon.gather()
    assert result.auto_runs == 2 and result.drafts_working == 2
    assert result.drafts_working == result.auto_runs


@pytest.mark.asyncio
async def test_cap_respected(db, monkeypatch):
    _cfg(monkeypatch, mode="live", cap=2)
    mod = _FakeModule(
        autoruns=[
            {"company": "A", "contact": "a", "draft_summary": "s"},
            {"company": "B", "contact": "b", "draft_summary": "s"},
            {"company": "C", "contact": "c", "draft_summary": "s"},  # would be a 3rd
        ],
    )
    mon, _ = _mon(db, mod)
    result = await mon.gather()
    assert result.auto_runs == 2
    assert _autorun_calls(mod) == 2  # stopped at the cap, never dispatched the 3rd


@pytest.mark.asyncio
async def test_undelivered_nudge_not_marked_and_counts_as_failure(db, monkeypatch):
    # A FAILED nudge → account NOT marked and errors≥1 (a delivery failure is a
    # job-health failure). Drives via the auto-run's Staged outcome (no census).
    _cfg(monkeypatch, mode="live")
    mod = _FakeModule(autoruns=[{"company": "Zeta", "contact": "z@zeta.dev", "draft_summary": "s"}])
    mon, pipe = _mon(db, mod, pipe_status=OutreachStatus.FAILED)
    result = await mon.gather()
    assert result.nudged == 0 and len(pipe.sent) == 1  # attempted, not delivered
    assert result.errors >= 1  # FAILED delivery counts as a job-health failure
    assert not await observations.exists_by_hash(
        db, source="recon", content_hash=_nudge_hash("Zeta")
    )


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
async def test_runner_records_failure_on_unreachable_bridge(monkeypatch):
    # An unreachable bridge surfaces errors=1 → the runner records a job-health FAILURE
    # (not a clean skip / false success). Keeps a persistent outage visible via PR #1428
    # + the gap detector. mode="live" proves it is mode-independent.
    from genesis.surplus.jobs import runners

    calls = {"fail": [], "success": []}
    monkeypatch.setattr(runners, "record_failure", lambda k, m=None: calls["fail"].append((k, m)))
    monkeypatch.setattr(runners, "record_success", lambda k: calls["success"].append(k))

    class _StubMon:
        async def gather(self):
            return CareerOutreachResult(
                mode="live",
                health_ok=False,
                errors=1,
                details=["reasoning module unreachable (health check failed)"],
            )

    class _Sched:
        _career_outreach_monitor = _StubMon()
        _event_bus = None

    await runners.run_career_outreach_monitor(_Sched())
    assert calls["fail"] and calls["fail"][0][0] == "career_outreach_monitor"
    assert not calls["success"]


@pytest.mark.asyncio
async def test_pause_midtick_halts_dispatches_and_nudge(db, monkeypatch):
    # Codex P1: an owner /pause mid-tick stops the remaining auto-run dispatches AND the
    # nudge for what was already staged this tick.
    _cfg(monkeypatch, mode="live", cap=3)
    mod = _FakeModule(
        autoruns=[
            {"company": "A", "contact": "a", "draft_summary": "s"},
            {"company": "B", "contact": "b", "draft_summary": "s"},
        ],
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
async def test_module_reported_error_counts_as_failure(db, monkeypatch):
    # Codex P2: a module-reported {"error"} in the auto-run payload increments errors
    # so a persistent module error surfaces as a job-health failure.
    _cfg(monkeypatch, mode="live")
    mon, _ = _mon(db, _FakeModule(autorun_payload_error="auth failed"))
    result = await mon.gather()
    assert result.errors >= 1


@pytest.mark.asyncio
async def test_rejected_nudge_is_not_an_error(db, monkeypatch):
    # Codex r3 P2: submit_raw skips quiet-hours governance so IGNORED never occurs; a
    # REJECTED nudge is pipeline dedup (the owner already got an identical one) — NOT a
    # failure, and not re-marked (a harmless re-derive next tick).
    _cfg(monkeypatch, mode="live")
    mod = _FakeModule(autoruns=[{"company": "Zeta", "contact": "z@zeta.dev", "draft_summary": "s"}])
    mon, pipe = _mon(db, mod, pipe_status=OutreachStatus.REJECTED)
    result = await mon.gather()
    assert result.errors == 0 and result.nudged == 0 and len(pipe.sent) == 1
    assert not await observations.exists_by_hash(
        db, source="recon", content_hash=_nudge_hash("Zeta")
    )


@pytest.mark.asyncio
async def test_missing_pipeline_nudge_counts_as_failure(db, monkeypatch):
    # No pipeline → the nudge cannot be delivered → a job-health failure.
    _cfg(monkeypatch, mode="live")
    mod = _FakeModule(autoruns=[{"company": "Zeta", "contact": "z@zeta.dev", "draft_summary": "s"}])
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
    mon, _ = _mon(db, _FakeModule(autorun_malformed=True))
    result = await mon.gather()
    assert result.errors >= 1 and result.auto_runs == 0


@pytest.mark.asyncio
async def test_autorun_missing_company_counts_as_failure(db, monkeypatch):
    # Codex r3 P2: an auto-run reply missing 'company' is a protocol failure → errors≥1.
    _cfg(monkeypatch, mode="live")
    mon, _ = _mon(db, _FakeModule(autoruns=[{"draft_summary": "x"}]))
    result = await mon.gather()
    assert result.errors >= 1 and result.auto_runs == 0


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


# ── C: career bite-relay (pipeline-advance owner nudge, independent lever) ──────


async def _bite_count(db) -> int:
    row = await (
        await db.execute("SELECT COUNT(*) FROM observations WHERE type = 'career_bite'")
    ).fetchone()
    return row[0]


@pytest.mark.asyncio
async def test_bite_relay_off_does_not_read_or_nudge(db, monkeypatch):
    # bite_relay off (and auto-run off) → the data module is never read, nothing nudged.
    _cfg(monkeypatch, mode="off", bite_mode="off")
    data = _FakeDataModule(pipeline={"in_conversation": [_entry(1, "Acme")]})
    mon, pipe = _mon_bite(db, data)
    result = await mon.gather()
    assert result.bite_mode == "off" and result.bites == 0
    assert data.ops == [] and pipe.sent == []


@pytest.mark.asyncio
async def test_bite_relay_observe_seeds_ledger_no_nudge(db, monkeypatch):
    # observe → READ + SEED the dedup ledger (record career_bite obs) but NEVER nudge,
    # so the first live tick won't fire a backlog for already-advanced companies.
    _cfg(monkeypatch, mode="off", bite_mode="observe")
    data = _FakeDataModule(pipeline={"in_conversation": [_entry(1, "Acme"), _entry(2, "Globex")]})
    mon, pipe = _mon_bite(db, data)
    result = await mon.gather()
    assert result.bite_mode == "observe" and result.bites == 0
    assert pipe.sent == []  # observe never nudges
    assert await _bite_count(db) == 2  # both seeded
    assert await observations.exists_by_hash(
        db, source="recon", content_hash=_bite_hash(1, "in_conversation"), unresolved_only=False
    )


@pytest.mark.asyncio
async def test_bite_relay_live_nudges_new_advance_and_marks(db, monkeypatch):
    _cfg(monkeypatch, mode="off", bite_mode="live")
    data = _FakeDataModule(pipeline={"interviewing": [_entry(7, "Initech")]})
    mon, pipe = _mon_bite(db, data)
    result = await mon.gather()
    assert result.bites == 1 and result.errors == 0
    assert len(pipe.sent) == 1
    body = pipe.sent[0][0]
    assert "Initech" in body and "interviewing" in body
    assert await observations.exists_by_hash(
        db, source="recon", content_hash=_bite_hash(7, "interviewing"), unresolved_only=False
    )


@pytest.mark.asyncio
async def test_bite_relay_dedup_does_not_renudge(db, monkeypatch):
    # A (company, stage) already in the ledger (even RESOLVED — point-event dedup uses
    # unresolved_only=False) must NOT re-nudge.
    await observations.create(
        db,
        id="seed",
        source="recon",
        type="career_bite",
        content="bite:Initech:interviewing",
        priority="low",
        created_at="2026-08-01T00:00:00Z",
        content_hash=_bite_hash(7, "interviewing"),
        skip_if_duplicate=True,
    )
    _cfg(monkeypatch, mode="off", bite_mode="live")
    data = _FakeDataModule(pipeline={"interviewing": [_entry(7, "Initech")]})
    mon, pipe = _mon_bite(db, data)
    result = await mon.gather()
    assert result.bites == 0 and pipe.sent == []  # already relayed → suppressed


@pytest.mark.asyncio
async def test_bite_relay_dedup_survives_marker_resolution(db, monkeypatch):
    # THE point-event-dedup lock (architect's sharpest finding): a stage advance is a
    # one-time event, so a RESOLVED marker must STILL suppress a re-nudge. The relay
    # calls exists_by_hash(unresolved_only=False); under the re-emittable-marker pattern
    # (unresolved_only=True, copied from _nudge) a resolved marker would no longer match
    # and the owner would get a surprise resurrection nudge. Seed + RESOLVE, expect no re-nudge.
    await observations.create(
        db,
        id="bite-seed",
        source="recon",
        type="career_bite",
        content="bite:Hooli:offer",
        priority="low",
        created_at="2026-07-01T00:00:00Z",
        content_hash=_bite_hash(9, "offer"),
        skip_if_duplicate=True,
    )
    await observations.resolve(
        db, "bite-seed", resolved_at="2026-07-02T00:00:00Z", resolution_notes="closed out"
    )
    _cfg(monkeypatch, mode="off", bite_mode="live")
    data = _FakeDataModule(pipeline={"offer": [_entry(9, "Hooli")]})
    mon, pipe = _mon_bite(db, data)
    result = await mon.gather()
    assert result.bites == 0 and pipe.sent == []  # resolved marker STILL dedups (point event)


@pytest.mark.asyncio
async def test_bite_relay_only_engaged_stages_fire(db, monkeypatch):
    # A company in a pre-engagement stage (researching/outreach_sent) or terminal
    # (closed) must NOT nudge; only advances into BITE_STAGES do.
    assert set(BITE_STAGES) == {"in_conversation", "interviewing", "offer"}
    _cfg(monkeypatch, mode="off", bite_mode="live")
    data = _FakeDataModule(
        pipeline={
            "researching": [_entry(1, "Early")],
            "outreach_sent": [_entry(2, "Pitched")],
            "closed": [_entry(3, "Done")],
            "in_conversation": [_entry(4, "Talking")],
        }
    )
    mon, pipe = _mon_bite(db, data)
    result = await mon.gather()
    assert result.bites == 1 and len(pipe.sent) == 1
    assert "Talking" in pipe.sent[0][0]
    assert "Early" not in pipe.sent[0][0] and "Done" not in pipe.sent[0][0]


@pytest.mark.asyncio
async def test_bite_relay_rejected_nudge_marks_relayed(db, monkeypatch):
    # Codex #1590 F2 (crash-window close): a REJECTED nudge means the pipeline's dedup
    # found an identical recent (company, stage) nudge — the owner ALREADY got this exact
    # advance. Treat it as relayed and WRITE THE MARKER, so a later tick (after the 24h
    # pipeline dedup expires) never re-delivers the same point-event. Not a fresh bite,
    # not a failure.
    _cfg(monkeypatch, mode="off", bite_mode="live")
    data = _FakeDataModule(pipeline={"offer": [_entry(9, "Hooli")]})
    mon, pipe = _mon_bite(db, data, pipe_status=OutreachStatus.REJECTED)
    result = await mon.gather()
    assert result.bites == 0 and result.errors == 0  # not a fresh bite, not a failure
    assert len(pipe.sent) == 1  # nudge was attempted
    assert await observations.exists_by_hash(
        db, source="recon", content_hash=_bite_hash(9, "offer"), unresolved_only=False
    )  # marker written on REJECTED → next tick's seen-check skips it (crash-window closed)


@pytest.mark.asyncio
async def test_bite_relay_nudge_dedup_key_is_full_id_hash(db, monkeypatch):
    # Codex #1590 F2 (architect + security follow-up): BOTH of submit_raw's dedup queries
    # (primary topic; secondary content-hash of context[:200]) must key on the FULL-id
    # marker hash (_bite_hash), NOT the id-free text (name+stage collides across distinct
    # companies) and NOT a raw str(company_id)[:100] prefix (two ids sharing a >100-char
    # prefix collide after truncation). Combined with mark-on-REJECTED, either collision
    # would permanently suppress a DISTINCT advance; the full-id hash cannot collide. The
    # delivered message is the `text` arg (submit_raw delivers text, not context), so this
    # sets dedup identity only, not what the owner sees.
    _cfg(monkeypatch, mode="off", bite_mode="live")
    data = _FakeDataModule(pipeline={"offer": [_entry(9, "Hooli")]})
    mon, pipe = _mon_bite(db, data)
    await mon.gather()
    text, request = pipe.sent[0]
    key = _bite_hash(9, "offer")  # the permanent marker hash — the SAME key
    assert request.topic == f"Career bite: {key}"  # primary dedup keyed on the full-id hash
    assert request.context.startswith(f"{key}\n")  # secondary content-hash keyed on it too
    assert "Hooli" in text  # owner still sees the human-readable name
    assert request.context.split("\n", 1)[1] == text  # delivered text unchanged


@pytest.mark.asyncio
async def test_bite_relay_malformed_stage_bucket_surfaces_failure(db, monkeypatch):
    # Codex #1590 F4: a PRESENT-but-wrong-type engaged-stage bucket (a partial jerbs
    # schema change) is surfaced as a job-health failure, NOT silently dropped as
    # "no new advances" — distinguished from a legitimately-absent stage.
    _cfg(monkeypatch, mode="off", bite_mode="live")
    data = _FakeDataModule(pipeline={"offer": {"items": [_entry(9, "Hooli")]}})  # dict, not list
    mon, pipe = _mon_bite(db, data)
    result = await mon.gather()
    assert result.errors >= 1 and result.bites == 0
    assert pipe.sent == []


@pytest.mark.asyncio
async def test_bite_relay_falsy_wrong_type_bucket_is_malformed(db, monkeypatch):
    # Codex #1590 F4 / security #3 (strict): a present-but-wrong-type bucket that is ALSO
    # falsy ({} / "" / 0 / False) must STILL surface as malformed (schema drift), not be
    # silently absorbed as an empty stage. Only None (absent) and a list (possibly empty)
    # are legitimate — an empty DICT is a wrong-type drift, not a legitimately-empty stage.
    _cfg(monkeypatch, mode="off", bite_mode="live")
    data = _FakeDataModule(pipeline={"offer": {}})  # empty dict — wrong type AND falsy
    mon, pipe = _mon_bite(db, data)
    result = await mon.gather()
    assert result.errors >= 1 and result.bites == 0
    assert pipe.sent == []


@pytest.mark.asyncio
async def test_bite_relay_malformed_entry_surfaces_failure(db, monkeypatch):
    # Codex #1590 F4: a non-dict entry inside a valid list bucket is surfaced (errors),
    # not silently skipped — but the valid sibling entry still nudges.
    _cfg(monkeypatch, mode="off", bite_mode="live")
    data = _FakeDataModule(pipeline={"offer": ["not-a-dict", _entry(9, "Hooli")]})
    mon, pipe = _mon_bite(db, data)
    result = await mon.gather()
    assert result.errors >= 1
    assert result.bites == 1 and len(pipe.sent) == 1  # the valid entry still relays


@pytest.mark.asyncio
async def test_bite_relay_strips_control_chars_from_company_name(db, monkeypatch):
    # Codex #1590 F5: a crafted company name with an embedded newline (line-forging) is
    # collapsed to a single space BEFORE render — html.escape alone (in _bite_nudge)
    # would NOT catch \n, so the parse_mode="HTML" Telegram sink could otherwise be
    # tricked into a forged notification line.
    _cfg(monkeypatch, mode="off", bite_mode="live")
    data = _FakeDataModule(pipeline={"offer": [_entry(9, "Acme\nFAKE LINE")]})
    mon, pipe = _mon_bite(db, data)
    result = await mon.gather()
    assert result.bites == 1
    body = pipe.sent[0][0]
    assert "\n" not in body  # the injected newline was collapsed → single-line message
    assert "Acme FAKE LINE" in body  # collapsed to a space, substance preserved


@pytest.mark.asyncio
async def test_bite_relay_runs_while_autorun_off(db, monkeypatch):
    # The load-bearing decoupling: auto-run mode="off", bite-relay live → the relay
    # runs and nudges even though the (policy-blocked) auto-run is gated off.
    _cfg(monkeypatch, mode="off", bite_mode="live")
    data = _FakeDataModule(pipeline={"offer": [_entry(9, "Hooli")]})
    mon, pipe = _mon_bite(db, data)
    result = await mon.gather()
    assert result.mode == "off" and result.bite_mode == "live"
    assert result.bites == 1 and len(pipe.sent) == 1


@pytest.mark.asyncio
async def test_bite_relay_unhealthy_data_module_is_clean_skip(db, monkeypatch):
    # An unhealthy DATA bridge (the HTTP read service down when the search is dormant)
    # is a CLEAN SKIP, NOT a job-health failure — avoids a daily false alarm.
    _cfg(monkeypatch, mode="off", bite_mode="live")
    data = _FakeDataModule(healthy=False, pipeline={"offer": [_entry(9, "Hooli")]})
    mon, pipe = _mon_bite(db, data)
    result = await mon.gather()
    assert result.errors == 0 and result.bites == 0 and pipe.sent == []
    assert data.ops == []  # never even read the pipeline


@pytest.mark.asyncio
async def test_bite_relay_pipeline_error_dict_is_failure(db, monkeypatch):
    # The service is UP (health ok) but the read returns {"error"} → a genuine failure
    # (errors=1 → job-health), distinct from the dormant unhealthy-skip above.
    _cfg(monkeypatch, mode="off", bite_mode="live")
    data = _FakeDataModule(error="pipeline query failed")
    mon, pipe = _mon_bite(db, data)
    result = await mon.gather()
    assert result.errors == 1 and result.bites == 0 and pipe.sent == []


@pytest.mark.asyncio
async def test_bite_relay_bad_shape_is_failure(db, monkeypatch):
    # Service up but the reply's 'pipeline' is not a dict (a schema change) → surface a
    # failure rather than silently reading nothing.
    _cfg(monkeypatch, mode="off", bite_mode="live")
    data = _FakeDataModule(bad_shape=True)
    mon, pipe = _mon_bite(db, data)
    result = await mon.gather()
    assert result.errors == 1 and result.bites == 0


@pytest.mark.asyncio
async def test_bite_relay_pipeline_read_raise_is_failure_not_crash(db, monkeypatch):
    # WARNING #2: execute_operation normally returns an error DICT, but a malformed
    # non-JSON body makes resp.json() RAISE. The bite-relay must catch it (like the
    # health-check guard) → errors=1, never let it escape gather().
    _cfg(monkeypatch, mode="off", bite_mode="live")
    data = _FakeDataModule(raise_op=True)
    mon, pipe = _mon_bite(db, data)
    result = await mon.gather()  # must NOT raise
    assert result.errors == 1 and result.bites == 0 and pipe.sent == []


@pytest.mark.asyncio
async def test_bite_relay_read_raise_preserves_autorun_result(db, monkeypatch):
    # The "independent levers" contract: a RAISED bite-relay read must not discard the
    # auto-run branch's result. Both live; the relay read raises; the auto-run still staged.
    _cfg(monkeypatch, mode="live", bite_mode="live")
    reasoning = _FakeModule(autoruns=[{"company": "A", "contact": "a", "draft_summary": "s"}])
    data = _FakeDataModule(raise_op=True)
    mon, _ = _mon_bite(db, data, reasoning_module=reasoning)
    result = await mon.gather()
    assert result.auto_runs == 1  # auto-run result survived the relay's exception
    assert result.errors >= 1  # the relay read failure is still counted


@pytest.mark.asyncio
async def test_bite_relay_caps_total_candidates_scanned(db, monkeypatch):
    # WARNING #1: the per-tick work (a DB op per candidate) is bounded independently of
    # the message cap, so a buggy/oversized data_module response can't drive unbounded DB
    # work. observe (which ignores the MESSAGE cap) still respects this scan ceiling.
    from genesis.recon import career_outreach as co

    monkeypatch.setattr(co, "_MAX_BITE_CANDIDATES_PER_TICK", 2, raising=False)
    _cfg(monkeypatch, mode="off", bite_mode="observe")
    data = _FakeDataModule(
        pipeline={"in_conversation": [_entry(i, f"C{i}") for i in range(1, 6)]}  # 5 entries
    )
    mon, _ = _mon_bite(db, data)
    await mon.gather()
    assert await _bite_count(db) == 2  # scan truncated to the ceiling, not all 5 seeded


@pytest.mark.asyncio
async def test_bite_relay_truncates_long_company_name(db, monkeypatch):
    # NOTE 1: an adversarial entry with a very long name must not bloat the observations
    # row unboundedly — the stored name is length-capped.
    _cfg(monkeypatch, mode="off", bite_mode="observe")
    data = _FakeDataModule(pipeline={"offer": [{"id": 1, "name": "X" * 5000}]})
    mon, _ = _mon_bite(db, data)
    await mon.gather()
    row = await (
        await db.execute("SELECT content FROM observations WHERE type='career_bite'")
    ).fetchone()
    assert len(row[0]) < 500  # bounded, not ~5000


@pytest.mark.asyncio
async def test_bite_relay_enabled_but_no_data_module_is_failure(db, monkeypatch):
    # An ENABLED lever (observe/live) whose data_module is empty/unresolvable delivers
    # NOTHING — surface a job-health FAILURE (errors=1), not a silent green no-op that a
    # log warning nobody watches would hide. (The genuine generic-install case ships
    # bite_mode=off and never reaches _run_bite_relay at all.)
    _cfg(monkeypatch, mode="off", bite_mode="live", data_module="")
    mon, pipe = _mon_bite(db, _FakeDataModule())
    result = await mon.gather()
    assert result.errors == 1 and result.bites == 0 and pipe.sent == []


@pytest.mark.asyncio
async def test_bite_relay_nudge_failed_not_marked(db, monkeypatch):
    # A FAILED nudge → errors=1 AND the marker is NOT written (so the advance re-tries
    # next tick), mirroring the auto-run nudge's mark-only-on-delivery contract.
    _cfg(monkeypatch, mode="off", bite_mode="live")
    data = _FakeDataModule(pipeline={"offer": [_entry(9, "Hooli")]})
    mon, pipe = _mon_bite(db, data, pipe_status=OutreachStatus.FAILED)
    result = await mon.gather()
    assert result.errors == 1 and result.bites == 0 and len(pipe.sent) == 1
    assert not await observations.exists_by_hash(
        db, source="recon", content_hash=_bite_hash(9, "offer"), unresolved_only=False
    )


@pytest.mark.asyncio
async def test_bite_relay_rejected_nudge_is_not_error(db, monkeypatch):
    # A REJECTED nudge (pipeline dedup) is not a failure and is not marked.
    _cfg(monkeypatch, mode="off", bite_mode="live")
    data = _FakeDataModule(pipeline={"offer": [_entry(9, "Hooli")]})
    mon, pipe = _mon_bite(db, data, pipe_status=OutreachStatus.REJECTED)
    result = await mon.gather()
    assert result.errors == 0 and result.bites == 0 and len(pipe.sent) == 1


@pytest.mark.asyncio
async def test_bite_relay_caps_nudges_per_tick(db, monkeypatch):
    # A per-tick nudge cap bounds a burst (e.g. a direct off→live with many advances):
    # excess advances are NOT marked, so they surface on a later tick (loud-truncation,
    # never silently dropped). Mirrors the auto-run's max_auto_runs_per_tick.
    _cfg(monkeypatch, mode="off", bite_mode="live", bite_cap=2)
    data = _FakeDataModule(
        pipeline={"in_conversation": [_entry(1, "A"), _entry(2, "B"), _entry(3, "C")]}
    )
    mon, pipe = _mon_bite(db, data)
    result = await mon.gather()
    assert result.bites == 2 and len(pipe.sent) == 2  # capped at 2
    # the 3rd was NOT marked → re-surfaces next tick (deferred, not lost)
    assert not await observations.exists_by_hash(
        db, source="recon", content_hash=_bite_hash(3, "in_conversation"), unresolved_only=False
    )


@pytest.mark.asyncio
async def test_bite_relay_observe_ignores_nudge_cap(db, monkeypatch):
    # The cap bounds owner MESSAGES (live nudges); observe seeds all advances (no spam),
    # so a low cap must not stop observe from seeding the whole set.
    _cfg(monkeypatch, mode="off", bite_mode="observe", bite_cap=1)
    data = _FakeDataModule(
        pipeline={"in_conversation": [_entry(1, "A"), _entry(2, "B"), _entry(3, "C")]}
    )
    mon, pipe = _mon_bite(db, data)
    await mon.gather()
    assert pipe.sent == [] and await _bite_count(db) == 3  # all seeded despite cap=1


@pytest.mark.asyncio
async def test_bite_relay_skips_falsy_id_entry(db, monkeypatch):
    # A malformed row with an empty-string id is skipped (no nudge, no degenerate
    # `career_bite::stage` collision hash) AND surfaced as malformed (errors>=1, so a
    # schema drift that strips `id` from every entry can't stay green while dropping all
    # advances); a real-id sibling still fires.
    _cfg(monkeypatch, mode="off", bite_mode="live")
    data = _FakeDataModule(pipeline={"offer": [{"id": "", "name": "Malformed"}, _entry(9, "Good")]})
    mon, pipe = _mon_bite(db, data)
    result = await mon.gather()
    assert result.bites == 1 and len(pipe.sent) == 1
    assert "Good" in pipe.sent[0][0] and "Malformed" not in pipe.sent[0][0]
    assert result.errors >= 1  # the id-less entry is surfaced, not silently dropped


@pytest.mark.asyncio
async def test_bite_relay_whitespace_id_is_malformed(db, monkeypatch):
    # Codex #1590: a whitespace-only id passes the literal-empty check, but _bite_hash .strip()s
    # the key so " "/"\t"/etc. all collapse to the SAME empty-key hash for a stage → the first
    # marker would suppress every later whitespace-id advance. Surface as malformed + skip.
    _cfg(monkeypatch, mode="off", bite_mode="live")
    data = _FakeDataModule(pipeline={"offer": [{"id": "  ", "name": "Blank"}, _entry(9, "Good")]})
    mon, pipe = _mon_bite(db, data)
    result = await mon.gather()
    assert result.bites == 1 and len(pipe.sent) == 1  # only the real-id sibling nudges
    assert "Good" in pipe.sent[0][0] and "Blank" not in pipe.sent[0][0]
    assert result.errors >= 1  # the whitespace id is surfaced, not silently collided


@pytest.mark.asyncio
async def test_bite_relay_rechecks_pause_before_read(db, monkeypatch):
    # Codex #1590: the pipeline read is an external action. A /pause that lands after the
    # runner's initial check (e.g. during the long preceding auto-run dispatch) must halt
    # the relay BEFORE its health check + pipeline read, per the "pause between external
    # actions" contract. Clean skip — no read, no error, no nudge.
    _cfg(monkeypatch, mode="off", bite_mode="live")
    data = _FakeDataModule(pipeline={"offer": [_entry(9, "Hooli")]})
    mon, pipe = _mon_bite(db, data)
    mon._is_paused = lambda: True
    result = await mon.gather()
    assert data.ops == []  # paused → the external pipeline read never ran
    assert result.bites == 0 and result.errors == 0 and pipe.sent == []


@pytest.mark.asyncio
async def test_bite_relay_sanitizes_id_fallback_name(db, monkeypatch):
    # Codex #1590: when `name` is absent, the fallback "company {id}" interpolates a RAW
    # external company_id, which can itself carry control chars html.escape won't remove.
    # strip_control_chars must run on the WHOLE value, fallback included.
    _cfg(monkeypatch, mode="off", bite_mode="live")
    # id carries an embedded newline; name absent → the fallback path renders the id.
    data = _FakeDataModule(pipeline={"offer": [{"id": "77\ninjected"}]})
    mon, pipe = _mon_bite(db, data)
    result = await mon.gather()
    assert result.bites == 1
    body = pipe.sent[0][0]
    assert "\n" not in body  # the id's newline was collapsed by strip_control_chars
    assert "company 77 injected" in body  # fallback sanitized (newline → single space)


@pytest.mark.asyncio
async def test_bite_relay_fallback_fires_when_name_sanitizes_empty(db, monkeypatch):
    # A name that is non-empty but sanitizes to EMPTY (whitespace-/control-only) must STILL
    # fall through to the id-derived fallback — strip_control_chars also .strip()s, so gating
    # the fallback on the raw name would leave a blank "🎯 Career:  advanced to offer". The
    # owner must see "company {id}".
    _cfg(monkeypatch, mode="off", bite_mode="live")
    data = _FakeDataModule(pipeline={"offer": [{"id": 42, "name": "   \t "}]})
    mon, pipe = _mon_bite(db, data)
    result = await mon.gather()
    assert result.bites == 1
    body = pipe.sent[0][0]
    assert "company 42" in body  # fallback fired despite a non-empty-but-blank name


@pytest.mark.asyncio
async def test_bite_nudge_escapes_company_name_html(db, monkeypatch):
    # company_name flows from the EXTERNAL pipeline into a telegram nudge the outreach
    # path sends parse_mode="HTML" WITHOUT escaping. A crafted name (e.g. from a
    # malicious job posting jerbs ingested) must be HTML-escaped so it can't render as
    # live markup in the owner's trusted chat (same sink/class as the marketing reply-ping).
    _cfg(monkeypatch, mode="off", bite_mode="live")
    data = _FakeDataModule(pipeline={"offer": [{"id": 1, "name": '<a href="http://x">Evil</a>'}]})
    mon, pipe = _mon_bite(db, data)
    await mon.gather()
    body = pipe.sent[0][0]
    assert "<a href" not in body  # no raw tag survives
    assert "&lt;a href" in body  # escaped form present (content preserved)


@pytest.mark.asyncio
async def test_bite_and_autorun_both_run_and_merge(db, monkeypatch):
    # BOTH sub-capabilities live → both run, results merge (auto_runs from the auto-run,
    # bites from the relay), each health-gated on its OWN module.
    _cfg(monkeypatch, mode="live", bite_mode="live")
    reasoning = _FakeModule(autoruns=[{"company": "A", "contact": "a", "draft_summary": "s"}])
    data = _FakeDataModule(pipeline={"in_conversation": [_entry(5, "Talky")]})
    mon, pipe = _mon_bite(db, data, reasoning_module=reasoning)
    result = await mon.gather()
    assert result.auto_runs == 1 and result.bites == 1
    assert result.mode == "live" and result.bite_mode == "live"
    # one auto-run nudge + one bite nudge
    assert len(pipe.sent) == 2


# ── bite-relay ↔ runner job-health accounting (architect finding #2) ───────────


@pytest.mark.asyncio
async def test_runner_records_when_only_bite_relay_active(monkeypatch):
    # Architect finding #2: the auto-run mode is OFF but the bite-relay ran → the runner
    # must NOT skip recording (the old `mode=="off"` skip would hide the relay's health).
    from genesis.surplus.jobs import runners

    calls = {"fail": [], "success": []}
    monkeypatch.setattr(runners, "record_failure", lambda k, m=None: calls["fail"].append(k))
    monkeypatch.setattr(runners, "record_success", lambda k: calls["success"].append(k))

    class _StubMon:
        async def gather(self):
            return CareerOutreachResult(mode="off", bite_mode="live", bites=1)

    class _Sched:
        _career_outreach_monitor = _StubMon()
        _event_bus = None

    await runners.run_career_outreach_monitor(_Sched())
    assert calls["success"] == ["career_outreach_monitor"] and not calls["fail"]


@pytest.mark.asyncio
async def test_runner_skips_only_when_both_levers_off(monkeypatch):
    # The skip is now BOTH-off: only a fully-disabled monitor stays invisible in job-health.
    from genesis.surplus.jobs import runners

    calls = {"fail": [], "success": []}
    monkeypatch.setattr(runners, "record_failure", lambda k, m=None: calls["fail"].append(k))
    monkeypatch.setattr(runners, "record_success", lambda k: calls["success"].append(k))

    class _StubMon:
        async def gather(self):
            return CareerOutreachResult(mode="off", bite_mode="off")

    class _Sched:
        _career_outreach_monitor = _StubMon()
        _event_bus = None

    await runners.run_career_outreach_monitor(_Sched())
    assert not calls["success"] and not calls["fail"]


@pytest.mark.asyncio
async def test_runner_records_failure_on_bite_relay_error(monkeypatch):
    # A bite-relay read error (auto-run off) surfaces errors=1 → record FAILURE.
    from genesis.surplus.jobs import runners

    calls = {"fail": [], "success": []}
    monkeypatch.setattr(runners, "record_failure", lambda k, m=None: calls["fail"].append(k))
    monkeypatch.setattr(runners, "record_success", lambda k: calls["success"].append(k))

    class _StubMon:
        async def gather(self):
            return CareerOutreachResult(
                mode="off", bite_mode="live", errors=1, details=["bite-relay: pipeline read failed"]
            )

    class _Sched:
        _career_outreach_monitor = _StubMon()
        _event_bus = None

    await runners.run_career_outreach_monitor(_Sched())
    assert calls["fail"] and not calls["success"]


@pytest.mark.asyncio
async def test_runner_no_progress_warning_fires_despite_bites(monkeypatch):
    # The auto-run verifier refused every attempt (verify_failed>0, no auto_runs/nudged),
    # but the INDEPENDENT bite-relay delivered a bite. The auto-run no-progress warning
    # must STILL fire — bite activity is a separate sub-capability and must not mask a
    # persistently-broken auto-run verifier (Codex #1590 F3). (Was previously suppressed.)
    from genesis.surplus.jobs import runners

    calls = {"fail": [], "success": []}
    monkeypatch.setattr(runners, "record_failure", lambda k, m=None: calls["fail"].append(k))
    monkeypatch.setattr(runners, "record_success", lambda k: calls["success"].append(k))
    events: list = []

    class _Bus:
        async def emit(self, subsystem, severity, code, msg, **kw):
            events.append(code)

    class _StubMon:
        async def gather(self):
            return CareerOutreachResult(mode="live", bite_mode="live", verify_failed=1, bites=1)

    class _Sched:
        _career_outreach_monitor = _StubMon()
        _event_bus = _Bus()

    await runners.run_career_outreach_monitor(_Sched())
    # verify_failed is NOT a job-health failure (the gate working) → still success…
    assert calls["success"] == ["career_outreach_monitor"]
    # …but the no-progress warning FIRES: a bite is not auto-run progress.
    assert "career_outreach.no_progress" in events


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


def test_autorun_note_appended_to_autorun_prompt_only():
    # The overlay note is appended to the AUTO-RUN prompt when set, and NOT when blank.
    base = _autorun_prompt({})
    assert base == _AUTORUN_PROMPT  # blank note → prompt unchanged
    noted = _autorun_prompt({"autorun_note": "ZZZ_INSTALL_NOTE"})
    assert noted.startswith(_AUTORUN_PROMPT) and "ZZZ_INSTALL_NOTE" in noted


@pytest.mark.asyncio
async def test_autorun_note_rides_autorun_dispatch(db, monkeypatch):
    # The install note rides the auto-run dispatch (live). Capture the dispatched prompts.
    _cfg(monkeypatch, mode="live", autorun_note="ZZZ_INSTALL_NOTE")
    mod = _FakeModule(autoruns=[{"company": "Z", "contact": "z@z.dev", "draft_summary": "s"}])
    mon, _ = _mon(db, mod)
    await mon.gather()
    autorun_prompts = [c for c in mod.calls if c != _PROBE_PROMPT]
    assert autorun_prompts and all("ZZZ_INSTALL_NOTE" in c for c in autorun_prompts)


@pytest.mark.asyncio
async def test_autorun_note_does_not_ride_observe_probe(db, monkeypatch):
    # The note must NOT ride the observe probe — the probe path stays clean of any
    # install-specific outreach/gate vocabulary (which could trip the engine's hooks).
    _cfg(monkeypatch, mode="observe", autorun_note="ZZZ_INSTALL_NOTE")
    mod = _FakeModule()
    mon, _ = _mon(db, mod)
    await mon.gather()
    assert mod.calls == [_PROBE_PROMPT]  # only the clean probe, note nowhere near it


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
    )
    mon, _ = _mon(db, mod)
    result = await mon.gather()
    assert result.auto_runs == 1 and _autorun_calls(mod) == 2


@pytest.mark.asyncio
async def test_autorun_dispatch_passes_max_turns(db, monkeypatch):
    # The auto-run dispatch carries max_turns=80 (the gated flow needs >25 turns).
    _cfg(monkeypatch, mode="live")
    mod = _FakeModule(autoruns=[{"company": "Z", "contact": "z@z.dev", "draft_summary": "s"}])
    mon, _ = _mon(db, mod)
    await mon.gather()
    autorun_params = [
        p for p, c in zip(mod.param_calls, mod.calls, strict=True) if c != _PROBE_PROMPT
    ]
    assert autorun_params and all(p.get("max_turns") == 80 for p in autorun_params)


@pytest.mark.asyncio
async def test_verify_failed_blank_reason_not_counted_as_staged(db, monkeypatch):
    # Review NOTE 2: an empty-string verify_failed reason must be treated as
    # verify_failed (presence-check), NOT fall through to the staged path and
    # miscount a draft that never staged.
    _cfg(monkeypatch, mode="live", cap=3)
    mod = _FakeModule(autoruns=[{"verify_failed": "", "company": "A"}])
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
        mon, _ = _mon(db, _FakeModule(autoruns=[combo]))
        result = await mon.gather()
        assert result.errors >= 1 and result.auto_runs == 0 and result.verify_failed == 0


@pytest.mark.asyncio
async def test_empty_dict_reply_is_error(db, monkeypatch):
    # An empty dict reply is a protocol violation (no outcome) → error, not swallowed.
    _cfg(monkeypatch, mode="live", cap=3)
    mon, _ = _mon(db, _FakeModule(autoruns=[{}]))
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
    mod = _FakeModule(autoruns=[{"verify_failed": "x"}])
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


@pytest.mark.asyncio
async def test_runner_no_progress_warning_suppressed_on_error_tick(monkeypatch):
    # Codex P2: a MIXED tick (verify_failed>0 AND errors>0) records the hard FAILURE
    # and must NOT also emit the "verifier refused every attempt" no-progress warning
    # (two conflicting diagnoses — operators would chase the verifier, not the error).
    from genesis.surplus.jobs import runners

    calls = {"fail": [], "success": []}
    monkeypatch.setattr(runners, "record_failure", lambda k, m=None: calls["fail"].append(k))
    monkeypatch.setattr(runners, "record_success", lambda k: calls["success"].append(k))
    events: list = []

    class _Bus:
        async def emit(self, subsystem, severity, code, msg, **kw):
            events.append(code)

    class _StubMon:
        async def gather(self):
            return CareerOutreachResult(mode="live", verify_failed=1, errors=1)

    class _Sched:
        _career_outreach_monitor = _StubMon()
        _event_bus = _Bus()

    await runners.run_career_outreach_monitor(_Sched())
    assert calls["fail"] and not calls["success"]  # the hard failure is recorded
    assert "career_outreach.no_progress" not in events  # no second, misleading diagnosis


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
        '{"none_left":true,"company":"Acme"}',  # terminal signal must not carry company
        '{"error":"e","company":"Acme"}',  # terminal signal must not carry company
        '{"none_left":true,"company":123}',  # non-string company KEY present → reject
        '{"none_left":true,"company":"  "}',  # blank company KEY present → reject
        '{"none_left":true,"company":null}',  # null company KEY present → reject
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
    # ...INCLUDING the compound case: a JSON-shaped verdict AND a fenced payload
    # together (Codex P2 — the largest-end-index rule handles it).
    assert _parse_json('{"verdict":"pass"}\n```json\n{"company":"X"}\n```')["company"] == "X"
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


# ── PR #1590 round-4 findings: lever isolation, pause boundary, id typing, scan
# ── rotation, and permanent delivery recovery ─────────────────────────────────


@pytest.mark.asyncio
async def test_autorun_raise_does_not_disable_bite_relay(db, monkeypatch):
    # Codex #1590: the two sub-capabilities are INDEPENDENT, but an exception escaping
    # _run_autorun used to skip the relay for the whole daily tick (the runner caught it
    # one level up, so the pipeline was never read at all).
    _cfg(monkeypatch, mode="live", bite_mode="live")
    data = _FakeDataModule(pipeline={"offer": [_entry(9, "Hooli")]})
    mon, pipe = _mon_bite(db, data, reasoning_module=_FakeModule())

    async def _boom(*a, **k):
        raise RuntimeError("autorun exploded")

    mon._run_autorun = _boom
    result = await mon.gather()
    assert result.bites == 1  # the relay still ran and relayed
    assert data.ops == ["pipeline"]  # the pipeline WAS read
    assert result.errors == 1  # ...and the auto-run raise is a job-health failure
    assert any("auto-run raised" in d for d in result.details)


@pytest.mark.asyncio
async def test_bite_relay_raise_does_not_discard_autorun(db, monkeypatch):
    # The mirror direction: a raise escaping _run_bite_relay must not discard the
    # independently-computed auto-run result.
    _cfg(monkeypatch, mode="live", bite_mode="live")
    reasoning = _FakeModule(autoruns=[{"company": "A", "contact": "a", "draft_summary": "s"}])
    data = _FakeDataModule(pipeline={})
    mon, _pipe = _mon_bite(db, data, reasoning_module=reasoning)

    async def _boom(*a, **k):
        raise RuntimeError("relay exploded")

    mon._run_bite_relay = _boom
    result = await mon.gather()
    assert result.auto_runs == 1  # the auto-run result survived
    assert result.errors == 1
    assert any("bite-relay raised" in d for d in result.details)


@pytest.mark.asyncio
async def test_bite_relay_rechecks_pause_after_health_before_read(db, monkeypatch):
    # Codex #1590: the health probe is ITSELF an awaited external call. A /pause landing
    # while it is in flight must stop the pipeline read — "pause is rechecked BETWEEN
    # external actions" and health-probe -> read is such a boundary.
    _cfg(monkeypatch, mode="off", bite_mode="live")
    data = _FakeDataModule(pipeline={"offer": [_entry(9, "Hooli")]})
    mon, pipe = _mon_bite(db, data)

    state = {"paused": False}

    async def _health_then_pause():
        state["paused"] = True  # /pause arrives DURING the health await
        return True

    data.check_health_cached = _health_then_pause
    mon._is_paused = lambda: state["paused"]

    result = await mon.gather()
    assert data.ops == []  # the external read never started
    assert result.bites == 0 and result.errors == 0 and pipe.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", [{"a": 1}, [1, 2], (1, 2), True, False, 1.5, 9.0, float("nan")])
async def test_bite_relay_rejects_non_scalar_id(db, monkeypatch, bad_id):
    # Codex #1590 + review: an id must be a STABLE scalar. A container's str() is a repr
    # whose ordering can change between identical responses (a new hash -> a repeat
    # nudge); bool is not an identifier; a float is not either — `9` and `9.0` hash
    # differently, so an int->float serializer change would re-key every marker, and
    # every NaN id collapses to the single key 'nan'. All malformed: surfaced, never
    # dedup'd, never sent.
    _cfg(monkeypatch, mode="off", bite_mode="live")
    data = _FakeDataModule(
        pipeline={"offer": [{"id": bad_id, "name": "Weird"}, _entry(9, "Hooli")]}
    )
    mon, pipe = _mon_bite(db, data)
    result = await mon.gather()
    assert result.errors >= 1  # surfaced, not silently dropped
    assert result.bites == 1 and len(pipe.sent) == 1  # the valid sibling still relays
    assert await _bite_count(db) == 1  # no marker written for the malformed entry


@pytest.mark.asyncio
async def test_bite_entry_id_normalizes_without_changing_dedup_identity(db):
    # The choke point must not alter an ACCEPTED id's marker identity — a change would
    # silently re-nudge every already-relayed advance on the first tick after deploy.
    from genesis.recon.career_outreach import _bite_entry_id

    for raw in ("acme", "Acme", " acme ", "\tACME\n", 7, 0, -3):
        key = _bite_entry_id({"id": raw})
        assert key is not None
        assert _bite_hash(key, "offer") == _bite_hash(raw, "offer")
    # ...and every malformed shape resolves to None through the ONE helper.
    assert _bite_entry_id("not-a-dict") is None
    assert _bite_entry_id({}) is None
    assert _bite_entry_id({"id": None}) is None
    assert _bite_entry_id({"id": ""}) is None
    assert _bite_entry_id({"id": "   "}) is None
    assert _bite_entry_id({"id": {"x": 1}}) is None
    assert _bite_entry_id({"id": [1]}) is None
    assert _bite_entry_id({"id": True}) is None
    assert _bite_entry_id({"id": 9.0}) is None  # a float is not an identifier
    assert _bite_entry_id({"id": float("nan")}) is None


@pytest.mark.asyncio
async def test_bite_relay_scan_window_rotates_so_the_tail_progresses(db, monkeypatch):
    # Codex #1590: a FIXED prefix slice permanently excludes everything past the ceiling
    # when the same entries persist in an engaged stage — entry ceiling+1 is never
    # scanned at all. The window rotates one whole ceiling per daily tick instead.
    from genesis.recon import career_outreach as co

    monkeypatch.setattr(co, "_MAX_BITE_CANDIDATES_PER_TICK", 2, raising=False)
    _cfg(monkeypatch, mode="off", bite_mode="observe")
    entries = [_entry(i, f"C{i}") for i in range(1, 6)]  # 5 entries, ceiling 2

    day = {"n": 0}
    monkeypatch.setattr(co, "_tick_ordinal", lambda: day["n"], raising=False)

    seen: set[str] = set()
    for tick in range(3):  # ceil(5/2) = 3 ticks covers the whole list
        day["n"] = tick
        data = _FakeDataModule(pipeline={"in_conversation": list(entries)})
        mon, _ = _mon_bite(db, data)
        await mon.gather()
        rows = await (
            await db.execute("SELECT content FROM observations WHERE type = 'career_bite'")
        ).fetchall()
        seen = {r[0] for r in rows}
        assert len(seen) <= 2 * (tick + 1)  # never more than the ceiling per tick

    # Every entry was reached — a fixed prefix would have left C3/C4/C5 unseen forever.
    assert seen == {f"bite:C{i}:in_conversation" for i in range(1, 6)}


@pytest.mark.asyncio
async def test_bite_relay_recovers_marker_from_permanent_delivery_history(db, monkeypatch):
    # Codex #1590: the crash window (delivered, then died before _record_bite) was only
    # covered by the outreach pipeline's 24h dedup — a window NOT longer than this job's
    # daily retry interval, so it could lapse and re-deliver the point event. The
    # permanent outreach_history record closes it regardless of elapsed time.
    from genesis.db.crud import outreach as outreach_history
    from genesis.recon.career_outreach import _bite_topic

    _cfg(monkeypatch, mode="off", bite_mode="live")
    h = _bite_hash(9, "offer")
    await outreach_history.create(
        db,
        id="prior-send",
        signal_type="career_bite",
        topic=_bite_topic(h),
        category="notification",
        salience_score=0.85,
        channel="telegram",
        message_content="🎯 Career: Hooli advanced to offer",
        created_at="2020-01-01T00:00:00",  # long outside ANY dedup window
    )
    await outreach_history.record_delivery(db, "prior-send", delivered_at="2020-01-01T00:00:00")

    data = _FakeDataModule(pipeline={"offer": [_entry(9, "Hooli")]})
    mon, pipe = _mon_bite(db, data)
    result = await mon.gather()

    assert pipe.sent == []  # NOT re-sent — the point event was already delivered
    assert result.bites == 0 and result.errors == 0
    assert await _bite_count(db) == 1  # the missing marker was recovered
    assert any("1 marker(s) recovered from permanent delivery history" in d for d in result.details)


@pytest.mark.asyncio
async def test_bite_relay_delivery_recovery_is_not_blocked_by_the_nudge_cap(db, monkeypatch):
    # Recovery sends nothing, so it must not consume a nudge-cap slot — otherwise a
    # backlog of already-delivered advances would starve the recovery indefinitely.
    from genesis.db.crud import outreach as outreach_history
    from genesis.recon.career_outreach import _bite_topic

    _cfg(monkeypatch, mode="off", bite_mode="live", bite_cap=1)
    for cid in (1, 2):
        h = _bite_hash(cid, "offer")
        await outreach_history.create(
            db,
            id=f"prior-{cid}",
            signal_type="career_bite",
            topic=_bite_topic(h),
            category="notification",
            salience_score=0.85,
            channel="telegram",
            message_content="x",
            created_at="2020-01-01T00:00:00",
        )
        await outreach_history.record_delivery(
            db, f"prior-{cid}", delivered_at="2020-01-01T00:00:00"
        )

    data = _FakeDataModule(pipeline={"offer": [_entry(1, "A"), _entry(2, "B"), _entry(3, "C")]})
    mon, pipe = _mon_bite(db, data)
    result = await mon.gather()

    assert await _bite_count(db) == 3  # 2 recovered + 1 freshly relayed
    assert result.bites == 1 and len(pipe.sent) == 1  # only the un-delivered one was sent


@pytest.mark.asyncio
async def test_runner_reemits_error_event_for_an_isolated_sub_capability_raise(monkeypatch):
    # Review SHOULD-FIX: isolating a raise inside gather() takes it out of the runner's
    # own `except`, which was the ONLY emitter of the ERROR-severity
    # `career_outreach_monitor.failed` event (with the traceback). Without the re-emit an
    # isolated crash survives as a job-health counter + a log line and vanishes from the
    # ERROR stream the dashboard and health_errors read.
    from genesis.observability.events import Severity
    from genesis.surplus.jobs import runners

    calls = {"fail": [], "success": []}
    monkeypatch.setattr(runners, "record_failure", lambda k, m=None: calls["fail"].append(k))
    monkeypatch.setattr(runners, "record_success", lambda k: calls["success"].append(k))

    boom = RuntimeError("autorun exploded")

    class _Bus:
        def __init__(self):
            self.emitted = []

        async def emit(self, subsystem, severity, event, message, **kw):
            self.emitted.append((severity, event, kw))

    bus = _Bus()

    class _StubMon:
        async def gather(self):
            return CareerOutreachResult(
                mode="live", bite_mode="live", bites=1, errors=1,
                raised=boom, details=["auto-run raised: autorun exploded"],
            )

    class _Sched:
        _career_outreach_monitor = _StubMon()
        _event_bus = bus

    await runners.run_career_outreach_monitor(_Sched())

    assert calls["fail"] == ["career_outreach_monitor"] and not calls["success"]
    errors = [e for e in bus.emitted if e[0] == Severity.ERROR]
    assert len(errors) == 1
    assert errors[0][1] == "career_outreach_monitor.failed"
    # the traceback must ride along — that is the whole point of the event
    assert errors[0][2]  # failure_details(exc=...) produced payload


@pytest.mark.asyncio
async def test_runner_does_not_emit_error_event_for_a_plain_dispatch_error(monkeypatch):
    # The control: an ordinary errors=1 with NO raise (an adapter error dict) must keep
    # its pre-existing behaviour — record_failure only, no ERROR event.
    from genesis.observability.events import Severity
    from genesis.surplus.jobs import runners

    calls = {"fail": [], "success": []}
    monkeypatch.setattr(runners, "record_failure", lambda k, m=None: calls["fail"].append(k))
    monkeypatch.setattr(runners, "record_success", lambda k: calls["success"].append(k))

    class _Bus:
        def __init__(self):
            self.emitted = []

        async def emit(self, subsystem, severity, event, message, **kw):
            self.emitted.append((severity, event, kw))

    bus = _Bus()

    class _StubMon:
        async def gather(self):
            return CareerOutreachResult(
                mode="live", bite_mode="off", errors=1, details=["dispatch failed"]
            )

    class _Sched:
        _career_outreach_monitor = _StubMon()
        _event_bus = bus

    await runners.run_career_outreach_monitor(_Sched())
    assert calls["fail"] == ["career_outreach_monitor"]
    assert [e for e in bus.emitted if e[0] == Severity.ERROR] == []


@pytest.mark.asyncio
async def test_guarded_raise_carries_the_exception_out_to_the_result(db, monkeypatch):
    # The plumbing half: gather() must put the caught exception on `.raised` (and
    # _merge_results must carry it through) or the runner has nothing to re-emit.
    _cfg(monkeypatch, mode="live", bite_mode="live")
    data = _FakeDataModule(pipeline={})
    mon, _ = _mon_bite(db, data, reasoning_module=_FakeModule())
    boom = RuntimeError("autorun exploded")

    async def _raise(*a, **k):
        raise boom

    mon._run_autorun = _raise
    result = await mon.gather()
    assert result.raised is boom
    assert result.errors == 1


@pytest.mark.asyncio
async def test_bite_relay_recovery_details_are_one_line_whatever_the_count(db, monkeypatch):
    # Review NOTE: the recovery branch sits OUTSIDE the nudge cap, so a per-entry detail
    # line could append once per candidate (up to the scan ceiling, 200 chars of name
    # each) into `details` — which the runner joins into job_health's unbounded
    # `last_error`. It must emit ONE summary line carrying the count instead.
    from genesis.db.crud import outreach as outreach_history
    from genesis.recon.career_outreach import _bite_topic

    _cfg(monkeypatch, mode="off", bite_mode="live", bite_cap=1)
    entries = [_entry(i, f"C{i}") for i in range(1, 8)]
    for e in entries:
        h = _bite_hash(e["id"], "offer")
        await outreach_history.create(
            db,
            id=f"prior-{e['id']}",
            signal_type="career_bite",
            topic=_bite_topic(h),
            category="notification",
            salience_score=0.85,
            channel="telegram",
            message_content="x",
            created_at="2020-01-01T00:00:00",
        )
        await outreach_history.record_delivery(
            db, f"prior-{e['id']}", delivered_at="2020-01-01T00:00:00"
        )

    data = _FakeDataModule(pipeline={"offer": entries})
    mon, pipe = _mon_bite(db, data)
    result = await mon.gather()

    assert await _bite_count(db) == 7 and pipe.sent == []  # all 7 recovered, none re-sent
    recovery_lines = [d for d in result.details if "recovered" in d]
    assert len(recovery_lines) == 1  # ONE line, not seven
    assert "7 marker(s) recovered from permanent delivery history" in recovery_lines[0]
