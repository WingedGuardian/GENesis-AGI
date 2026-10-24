"""Pure report validation; synthetic profiles are not execution evidence."""

import copy
import uuid
from pathlib import Path

import pytest


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts/ci"))
    import ego_runtime_report

    return ego_runtime_report


def driver(outer, identifier, *, blocked=False, error=False, complete=True):
    native = {
        "session_id": str(uuid.uuid5(uuid.NAMESPACE_URL, identifier)),
        "is_error": error,
        "subtype": "error" if error else "success",
    }
    return {
        "outer_source": outer,
        "model": "sonnet",
        "complete": False if blocked else complete,
        "run_id": identifier,
        "native": None if blocked else native,
        "prompt_status": "65" if blocked else "0",
        "failure_kind": "reference_conflict" if blocked else None,
        "calls": [],
        "messages": []
        if blocked
        else [
            {"type": "ResultMessage", "data": native},
            {
                "type": "SystemMessage",
                "data": {
                    "subtype": "init",
                    "data": {
                        "session_id": native["session_id"],
                        "tools": ["mcp__fixture__claim_effect"],
                        "model": "fixture-resolved-model",
                    },
                },
            },
        ],
    }


def counters():
    return {
        "cursor": 0,
        "timer_reserved": 0,
        "timer_done": 0,
        "level": 1,
        "attempts": 1,
        "wake_at": 100,
    }


@pytest.fixture
def reports(module):
    metadata = {
        "status": "pass",
        "source_pin": "a" * 40,
        "sources": {"fixture": "b" * 64},
        "sources_unchanged": True,
        "sdk": "0.2.172",
        "native_binary": {"sha256": module.NATIVE_SHA, "version": "2.1.308 (Claude Code)"},
    }
    b = {
        **copy.deepcopy(metadata),
        "schema": "headlong-native-monolith/v1",
        "cleanup_confirmed": True,
        "comparison_manifest": module.manifest(),
        "cases": [],
    }
    c = {
        **copy.deepcopy(metadata),
        "schema": "ego-owned-matrix/v1",
        "cleanup_confirmed": True,
        "native_sessions": 13,
        "declared_population": {"native": {"comparison_manifest": module.manifest(), "cases": []}},
        "cases": [],
    }
    for case in module.CASES:
        left, right, observations = [], [], []
        for i in range(case.wakes):
            complete = not (
                case.blocked or case.fault or case.kind in {"provider_error", "handler_error"}
            )
            bd = driver(
                "monolith",
                f"B/{case.name}/{i}",
                blocked=case.blocked,
                error=case.kind == "provider_error",
                complete=complete,
            )
            cd = driver(
                "ego-owned-loop",
                f"C/{case.name}/{i}",
                blocked=case.blocked,
                error=case.kind == "provider_error",
                complete=complete,
            )
            effect = case.kind == "approved" and not case.blocked
            types = (
                ["error"]
                if case.kind in {"provider_error", "handler_error"} or case.fault == "observation"
                else ["observation"]
                if case.fault
                else ["final"]
            )
            if complete and not effect:
                types.append("idle")
            left.append(
                {
                    "driver": bd,
                    "backoff": {"level": 0 if case.fault == "final" else 1},
                    "wake_at": 100,
                    "step_returncode": 0,
                    "effect": "native-fixture-effect\n" if effect else None,
                    "state": {"status": "executed" if effect else "pending"},
                    "trajectory": [{"type": t, "run_id": bd["run_id"]} for t in types],
                }
            )
            right.append({"driver": cd})
            outcome = (
                "blocked"
                if case.blocked
                else "uncertain"
                if case.fault or case.kind == "handler_error"
                else "retry"
                if case.kind == "provider_error"
                else "refused"
                if i or case.kind == "pending"
                else "noop"
                if case.kind == "echo"
                else "completed"
            )
            observations.append({"outcome": outcome, "next": counters()})
        b["cases"].append({"scenario": case.name, "control_passed": True, "runs": left})
        c["cases"].append(
            {
                "scenario": case.name,
                "control_passed": True,
                "runs": right,
                "reports": [{"state": counters(), "observations": observations}],
                "after": {
                    "state": {"status": "executed" if effect else "pending"},
                    "effect": b"native-fixture-effect\n".hex() if effect else None,
                },
            }
        )
        c["declared_population"]["native"]["cases"].append(
            {
                "name": case.name,
                "attempts": case.wakes,
                "native_sessions": 0 if case.blocked else case.wakes,
                "outcomes": [o["outcome"] for o in observations],
            }
        )
    groups = {
        key: [{"id": f"{key}/{i}", "passed": True} for i in range(n)]
        for key, n in {"outcomes": 36, "bursts": 17, "guards": 6, "pacing": 1}.items()
    }
    c["state_controls"] = {
        "status": "pass",
        "rows": groups,
        "journals": [{} for _ in range(60)],
        "calibration": [{"rejected": True} for _ in range(144)],
    }
    r = {
        **copy.deepcopy(metadata),
        "schema": "ego-owned-recovery/v1",
        "challenge": None,
        "crashes": [{"stage": s, "passed": True, "scopes_closed": True} for s in module.CRASHES],
        "late_io": [{"phase": s, "passed": True, "scopes_closed": True} for s in module.LATE_IO],
        "calibration": {
            "init-adoption": {"passed": True, "missing_closure_rejected": True},
            "ordinary-effect": {"passed": True},
            "no-op-rejected": {"passed": False},
        },
    }
    t = {
        **copy.deepcopy(metadata),
        "schema": "ego-owned-timing/v1",
        "challenge": None,
        "declared_population": {"timer_waits": [5, 10, 20, 40, 40], "reactive_offsets": [3, 5, 8]},
        "calibration": [{"rejected": True} for _ in range(24)],
        "cases": [],
    }
    for name in module.TIMING:
        n = 6 if name == "timers" else 3 if name.startswith("reactive-") else 1
        t["cases"].append(
            {
                "scenario": name,
                "passed": True,
                "scopes_closed": True,
                **(
                    {"classification": "during-backoff", "offset": int(name.split("-")[1])}
                    if name.startswith("reactive-")
                    else {}
                ),
                "reports": [
                    {
                        "observations": [
                            {"evidence": {"driver": driver("ego-owned-loop", f"timing/{name}/{i}")}}
                            for i in range(n)
                        ]
                    }
                ],
            }
        )
    return [b, c, r, t]


def test_comparison_preserves_distinct_semantics_and_denominators(module, reports):
    result = module.comparison(*reports)
    assert result["status"] == "pass" and len(result["mechanics"]["cases"]) == 13
    provider = next(r for r in result["mechanics"]["cases"] if r["case"] == "provider_error")
    assert (
        provider["B"][0]["run_records"] == ["error"] and provider["B"][0]["outer_shell_exit"] == 0
    )
    assert provider["C"][0]["outcome"] == "retry"
    partial = next(r for r in result["mechanics"]["cases"] if r["case"] == "final_failure")
    assert partial["B"][0]["level"] == 0 and partial["C"][0]["outcome"] == "uncertain"
    assert result["pure_state"]["cells"] == 60 and result["recovery"]["process_interruptions"] == 12
    assert result["timing"]["native_sessions"] == 17 and "Codex execution" in result["unqualified"]
    assert all(result[key]["arm"] == "C" for key in ("pure_state", "recovery", "timing"))
    assert "B/C recovery and actual-clock parity" in result["unqualified"]


@pytest.mark.parametrize(
    "fault",
    [
        "missing_case",
        "duplicate_case",
        "bool_counter",
        "bool_native_count",
        "mixed_source",
        "mixed_profile",
        "model",
        "tool_profile",
        "typed_result",
        "duplicate_native",
        "recovery_cell",
        "recovery_scope",
        "reducer_id",
        "reducer_mutant",
        "timing_cell",
        "timing_profile",
        "no_backoff",
        "timing_mutant",
        "challenge",
        "bool_wait",
        "declared_case",
        "bool_attempt",
        "native_subtype",
        "unknown_record",
    ],
)
def test_incomplete_or_misbound_populations_fail_closed(module, reports, fault):
    b, c, r, t = reports
    if fault == "missing_case":
        b["cases"].pop()
    elif fault == "duplicate_case":
        b["cases"][-1]["scenario"] = b["cases"][0]["scenario"]
    elif fault == "bool_counter":
        c["cases"][0]["reports"][-1]["state"]["attempts"] = True
    elif fault == "bool_native_count":
        c["native_sessions"] = True
    elif fault == "mixed_source":
        r["source_pin"] = "c" * 40
    elif fault == "mixed_profile":
        t["native_binary"]["sha256"] = "c" * 64
    elif fault == "model":
        b["cases"][0]["runs"][0]["driver"]["model"] = "foreign"
    elif fault == "tool_profile":
        b["cases"][0]["runs"][0]["driver"]["messages"][1]["data"]["data"]["tools"] = ["unexpected"]
    elif fault == "typed_result":
        b["cases"][0]["runs"][0]["driver"]["messages"][0]["data"] = {"is_error": False}
    elif fault == "duplicate_native":
        b["cases"][1]["runs"][0]["driver"] = copy.deepcopy(b["cases"][0]["runs"][0]["driver"])
    elif fault == "recovery_cell":
        r["crashes"].pop()
    elif fault == "recovery_scope":
        r["late_io"][0]["scopes_closed"] = False
    elif fault == "reducer_id":
        c["state_controls"]["rows"]["outcomes"][1]["id"] = c["state_controls"]["rows"]["outcomes"][
            0
        ]["id"]
    elif fault == "reducer_mutant":
        c["state_controls"]["calibration"][0]["rejected"] = False
    elif fault == "timing_cell":
        t["cases"].pop()
    elif fault == "timing_profile":
        t["cases"][0]["reports"][0]["observations"][0]["evidence"]["driver"]["native"][
            "is_error"
        ] = True
    elif fault == "no_backoff":
        for row in t["cases"]:
            row["classification"] = "already-pending"
    elif fault == "timing_mutant":
        t["calibration"][0]["rejected"] = False
    elif fault == "challenge":
        r["challenge"] = {"error": "unaccounted native"}
    elif fault == "declared_case":
        c["declared_population"]["native"]["cases"].pop()
    elif fault == "bool_attempt":
        c["declared_population"]["native"]["cases"][0]["attempts"] = True
    elif fault == "native_subtype":
        b["cases"][0]["runs"][0]["driver"]["native"]["subtype"] = "unrecognized"
    elif fault == "unknown_record":
        b["cases"][0]["runs"][0]["trajectory"][0]["type"] = "unknown"
    else:
        t["declared_population"]["timer_waits"][0] = True
    with pytest.raises(ValueError):
        module.comparison(*reports)


@pytest.mark.parametrize(
    "fault",
    [
        "misbound-timers",
        "misbound-sigterm-backoff",
        "misbound-exclusive-writer",
        "unknown-class",
        "bool-class",
        "missing-class",
        "wrong-offset",
        "bool-offset",
        "missing-offset",
    ],
)
def test_actual_arrival_family_is_required(module, reports, fault):
    assert module.comparison(*reports)["status"] == "pass"
    timing = reports[3]
    arrivals = [row for row in timing["cases"] if row["scenario"].startswith("reactive-")]
    if fault.startswith("misbound-"):
        for row in arrivals:
            row["classification"] = "already-pending"
        target = fault.removeprefix("misbound-")
        next(row for row in timing["cases"] if row["scenario"] == target)["classification"] = (
            "during-backoff"
        )
    elif fault == "unknown-class":
        arrivals[0]["classification"] = "unknown"
    elif fault == "bool-class":
        arrivals[0]["classification"] = True
    elif fault == "missing-class":
        arrivals[0].pop("classification")
    elif fault == "wrong-offset":
        arrivals[0]["offset"] = 999
    elif fault == "bool-offset":
        arrivals[0]["offset"] = True
    else:
        arrivals[0].pop("offset")
    with pytest.raises((ValueError, KeyError)):
        module.comparison(*reports)


NATIVE_ATTEMPTS = [
    (name, 0)
    for name in (
        "effect",
        "noop",
        "refusal",
        "provider_error",
        "handler_error",
        "large_context",
        "reactive_noop",
        "restart_duplicate",
        "reference_continuity",
        "observation_failure",
        "final_failure",
    )
] + [("restart_duplicate", 1), ("reference_continuity", 1)]


@pytest.mark.parametrize("arm", [0, 1], ids=["B", "C"])
@pytest.mark.parametrize("blocked", ["reference_conflict", "reactive_reference_conflict"])
@pytest.mark.parametrize("name,attempt", NATIVE_ATTEMPTS)
def test_native_presence_is_bound_to_each_fixed_case(module, reports, arm, blocked, name, attempt):
    assert module.comparison(*reports)["status"] == "pass"
    cases = {row["scenario"]: row for row in reports[arm]["cases"]}
    native = cases[name]["runs"][attempt]["driver"]
    absent = cases[blocked]["runs"][0]["driver"]
    # Preserve run/trajectory identity and global session counts: only the
    # native-profile family correspondence is deliberately wrong.
    for field in ("native", "messages", "complete", "prompt_status", "failure_kind", "calls"):
        native[field], absent[field] = absent[field], native[field]
    with pytest.raises(ValueError):
        module.comparison(*reports)
