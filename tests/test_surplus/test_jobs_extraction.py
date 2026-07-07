"""Structural guard for the surplus/jobs extraction.

The job bodies live in ``genesis.surplus.jobs.*``; ``SurplusScheduler`` keeps
every original method name as a thin delegate (APScheduler callables, runtime
wiring, and tests all bind the methods). This locks that contract: every
delegate exists, is async, points at the right module function, and actually
invokes it — so a future edit can't silently orphan a job body or drop a
delegate.
"""

import inspect

import pytest

from genesis.surplus import scheduler as scheduler_mod
from genesis.surplus.jobs import dream, gates, gitnexus, runners
from genesis.surplus.scheduler import SurplusScheduler

# Delegate method on SurplusScheduler -> (jobs module, function name).
DELEGATES = {
    "brainstorm_check": (gates, "brainstorm_check"),
    "_recently_completed": (gates, "recently_completed"),
    "schedule_code_audit": (gates, "schedule_code_audit"),
    "schedule_code_index": (gates, "schedule_code_index"),
    "schedule_j9_eval_batch": (gates, "schedule_j9_eval_batch"),
    "_schedule_fresh_session_test": (gates, "schedule_fresh_session_test"),
    "schedule_model_eval": (gates, "schedule_model_eval"),
    "schedule_maintenance": (gates, "schedule_maintenance"),
    "schedule_analytical": (gates, "schedule_analytical"),
    "schedule_wing_audit": (gates, "schedule_wing_audit"),
    "schedule_cc_memory_staleness": (gates, "schedule_cc_memory_staleness"),
    "schedule_pipeline": (gates, "schedule_pipeline"),
    "run_db_integrity_check": (runners, "run_db_integrity_check"),
    "_alarm_db_integrity": (runners, "alarm_db_integrity"),
    "dispatch_follow_ups": (runners, "dispatch_follow_ups"),
    "run_recon_gather": (runners, "run_recon_gather"),
    "run_model_intelligence": (runners, "run_model_intelligence"),
    "run_skill_security_scan": (runners, "run_skill_security_scan"),
    "run_github_discovery": (runners, "run_github_discovery"),
    "run_models_md_synthesis": (runners, "run_models_md_synthesis"),
    "run_memory_extraction": (runners, "run_memory_extraction"),
    "run_dream_cycle": (dream, "run_dream_cycle"),
    "run_dream_synthesis_drain": (dream, "run_dream_synthesis_drain"),
    "run_gitnexus_reindex": (gitnexus, "run_gitnexus_reindex"),
    "run_gitnexus_strip": (gitnexus, "run_gitnexus_strip"),
}

# Extra positional args (beyond the scheduler) each delegate passes through.
_EXTRA_ARGS = {
    "_recently_completed": (None, 1),
    "schedule_pipeline": ("prompt_effectiveness",),
    "_alarm_db_integrity": ("integrity detail",),
}


def test_every_delegate_and_job_function_exists_and_is_async():
    for method_name, (module, func_name) in DELEGATES.items():
        method = getattr(SurplusScheduler, method_name, None)
        assert method is not None, f"SurplusScheduler.{method_name} delegate missing"
        assert inspect.iscoroutinefunction(method), f"SurplusScheduler.{method_name} not async"
        func = getattr(module, func_name, None)
        assert func is not None, f"{module.__name__}.{func_name} missing"
        assert inspect.iscoroutinefunction(func), f"{module.__name__}.{func_name} not async"


@pytest.mark.asyncio
async def test_delegates_invoke_their_module_function(monkeypatch):
    """Each delegate must call its jobs-module function (attribute looked up
    at call time on the module object, so monkeypatching the module works —
    the same seam a test would use to stub a job)."""
    calls = []
    sentinel = object()
    for method_name, (module, func_name) in DELEGATES.items():
        async def _spy(*args, _name=method_name, **kwargs):
            calls.append(_name)
            return sentinel
        monkeypatch.setattr(module, func_name, _spy)

    # Delegates never touch scheduler state themselves — no __init__ needed.
    sched = SurplusScheduler.__new__(SurplusScheduler)
    for method_name in DELEGATES:
        result = await getattr(sched, method_name)(*_EXTRA_ARGS.get(method_name, ()))
        if method_name in ("_recently_completed", "schedule_pipeline"):
            assert result is sentinel, f"{method_name} must propagate the return value"

    assert sorted(calls) == sorted(DELEGATES), (
        "delegates that never reached their module function: "
        f"{sorted(set(DELEGATES) - set(calls))}"
    )


def test_strip_gitnexus_block_reexport_is_the_same_object():
    assert scheduler_mod._strip_gitnexus_block is gitnexus._strip_gitnexus_block


def test_restart_safe_hourly_still_defined_in_scheduler():
    # Registration infra stays in scheduler.py (imported by test_scheduler.py).
    assert scheduler_mod._restart_safe_hourly.__module__ == "genesis.surplus.scheduler"
