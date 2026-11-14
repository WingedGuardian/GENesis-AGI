"""Pure report validation; synthetic profiles are not execution evidence."""

import base64
import copy
import hashlib
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest


def test_cli_rejects_incomplete_input():
    script = Path(__file__).resolve().parents[2] / "scripts/ci/ego_runtime_report.py"
    result = subprocess.run([sys.executable, str(script)], input="{}", text=True, capture_output=True, timeout=60)
    assert result.returncode != 0, "incomplete input must not silently succeed"
    assert json.loads(result.stdout)["status"] == "incomplete"


def test_workflow_runs_and_retains_the_data_only_consumer():
    import yaml

    workflow = yaml.safe_load((Path(__file__).resolve().parents[2] / ".github/workflows/headlong-controls.yml").read_text())
    steps = workflow["jobs"]["controls"]["steps"]
    consumer, = [step for step in steps if step.get("name") == "Read back separately qualified B and C populations"]
    text = consumer["run"]
    assert "headlong-controls python3 /fixture/ego_runtime_report.py" in text
    assert '--network none --read-only' in text and '--cap-drop ALL' in text
    assert 'docker start -ai "$COMPARISON_CONTAINER" < "$RUNNER_TEMP/ego-comparison-input.json"' in text
    assert 'headlong-bridge' in text and all(name in text for name in ('ego-matrix', 'ego-recovery', 'ego-timing'))
    assert 'json.loads' not in text, "transport must preserve duplicate-key evidence for the canonical reader"
    removal, = [step for step in steps if step.get("name") == "Remove only this evaluation container"]
    assert '"$COMPARISON_CONTAINER"' in removal["run"] and removal["if"] == "always()"
    upload, = [step for step in steps if step.get("uses", "").startswith("actions/upload-artifact")]
    assert 'ego-comparison-report.json' in upload["with"]["path"]
    assert steps.index(consumer) > next(i for i, step in enumerate(steps) if step.get("name") == "Run actual owned-loop clock and lifecycle controls")


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
        "invocation_id": identifier,
        "trigger_id": identifier + "/trigger",
        "prompt_sha256": hashlib.sha256(b"synthetic prompt").hexdigest(),
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


def identity():
    return {"source_pin": "a" * 40, "components": {k: "c" * 64 for k in ("state", "loop", "context", "bridge", "cases")},
            "roots": {k: "b" * 64 for k in ("headlong", "derived", "fixtures", "genesis")},
            "sdk": "0.2.172", "native_sha256": "b" * 64}


def counters():
    return {
        "cursor": 0,
        "timer_reserved": 0,
        "timer_done": 0,
        "level": 1,
        "attempts": 1,
        "wake_at": 100,
    }


def records(driver, kinds):
    return [{"type": "shellm-run", "step_id": driver["run_id"], "source": driver["outer_source"],
             "invocation_id": driver["invocation_id"], "trigger_step": driver["trigger_id"]}, *[
        {"type": kind, "run_id": driver["run_id"], "source": driver["outer_source"],
         **({"native_session_id": driver["native"]["session_id"]} if kind == "final" else {})} for kind in kinds]]


@pytest.fixture
def reports(module):
    from ego_owned_matrix import population as native_population
    from ego_owned_recovery import population as recovery_population
    from ego_owned_state_controls import population as state_population
    from ego_owned_timing import population as timing_population
    from ego_runtime_cases import CASES, manifest

    metadata = {
        "status": "pass",
        "source_pin": "a" * 40,
        "sources": {k: "b" * 64 for k in ("headlong", "derived", "fixtures", "genesis")},
        "sources_unchanged": True,
        "sdk": "0.2.172",
        "native_binary": {"sha256": "b" * 64, "version": "2.1.308 (Claude Code)"},
    }
    b = {
        **copy.deepcopy(metadata),
        "schema": "headlong-native-monolith/v1",
        "cleanup_confirmed": True,
        "comparison_manifest": manifest(),
        "cases": [],
    }
    c = {
        **copy.deepcopy(metadata),
        "schema": "ego-owned-matrix/v1",
        "cleanup_confirmed": True,
        "native_sessions": 13,
        "declared_population": {"native": {"comparison_manifest": manifest(), "cases": []}},
        "cases": [],
    }
    for case in CASES:
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
                if case.blocked or case.kind in {"provider_error", "handler_error"} or case.fault == "observation"
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
                    "trajectory": records(bd, types),
                }
            )
            right.append({"driver": cd, "raw_prompt": "synthetic prompt", "config": {
                "outer_source": "ego-owned-loop", "model": "sonnet", "scenario": case.name,
                "invocation_id": cd["invocation_id"], "trigger_id": cd["trigger_id"]}})
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
            evidence = ({"errors": ["ValueError: unfinished or failed protected handler"]}
                        if case.kind == "handler_error" or case.fault == "observation" else {"driver": copy.deepcopy(cd)})
            observations.append({"outcome": outcome, "next": counters(), "evidence": evidence})
        b["cases"].append({"scenario": case.name, "control_passed": True, "runs": left})
        c["cases"].append(
            {
                "scenario": case.name,
                "control_passed": True,
                "runs": right,
                "trajectory": [r for run in right for r in records(run["driver"], ["final"] if run["driver"]["complete"] else [])],
                "reports": [{"state": counters(), "observations": observations}],
                "manifest": {"source_pin": identity()["source_pin"], "sources": identity()["components"], "executor": "claude-code", "model": "sonnet"},
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
    inventory = state_population()
    c["declared_population"] = {"native": native_population(), "state": inventory}
    groups = {key: [{"id": v["id"] if isinstance(v, dict) else v, "passed": True} for v in inventory[key]]
              for key in ("outcomes", "bursts", "guards", "pacing")}
    c["state_controls"] = {
        "status": "pass",
        "schema": "ego-owned-state-controls/v1",
        "inventory": inventory,
        "rows": groups,
        "journals": [{} for _ in range(60)],
        "calibration": [{"id": cell["id"], "mutant": field, "rejected": True}
                        for cell in inventory["outcomes"] for field in ("cursor", "wake_at", "active", "observations")],
    }
    r = {
        **copy.deepcopy(metadata),
        "schema": "ego-owned-recovery/v1",
        "challenge": None,
        "declared_population": recovery_population(),
        "crashes": [{"stage": s, "passed": True, "scopes_closed": True} for s in ("before_intake", "after_intake", "prepared", "trigger", "config", "bound", "native", "claimed", "effect", "receipt", "collected", "committed")],
        "late_io": [{"phase": s, "passed": True, "scopes_closed": True} for s in ("db_close", "driver_open", "driver_short", "driver_close", "rename", "stdout", "stdout_flush")],
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
        "declared_population": timing_population(),
        "calibration": [{"case": name, "corruption": field, "rejected": True}
                        for name in ("timers", "reactive-3", "reactive-5", "reactive-8", "sigterm-backoff", "exclusive-writer")
                        for field in ("journal", "scopes_closed", "backends", "reports")],
        "cases": [],
    }
    for name in ("timers", "reactive-3", "reactive-5", "reactive-8", "sigterm-backoff", "exclusive-writer"):
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
    for report in (c, r):
        report["population_sha256"] = hashlib.sha256(json.dumps(report["declared_population"], sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()).hexdigest()
    for row in r["crashes"] + r["late_io"] + t["cases"]:
        row["manifest"] = {"source_pin": identity()["source_pin"], "sources": identity()["components"], "executor": "claude-code", "model": "sonnet"}
    for row in t["cases"]:
        row["manifest"].update(start=100, feed=[{"due": 100 + row["offset"]}] if "offset" in row else [])
        for observed in row["reports"][-1]["observations"]:
            observed.update(observed_at=100, next={"wake_at": 110})
        row["journal"] = [{"kind": "observation", "data": copy.deepcopy(observed)} for observed in row["reports"][-1]["observations"]]
        raw = "".join(json.dumps(record) + "\n" for record in row["journal"]).encode()
        row["artifacts"] = [{"path": "journal/answers.jsonl", "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "content_b64": base64.b64encode(raw).decode()}]
    return [b, c, r, t]


def test_comparison_preserves_distinct_semantics_and_denominators(module, reports):
    result = module.comparison(*reports, expected=identity())
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
        b["cases"][0]["runs"][0]["trajectory"][1]["type"] = "unknown"
    else:
        t["declared_population"]["timer_waits"][0] = True
    with pytest.raises(ValueError):
        module.comparison(*reports, expected=identity())


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
    assert module.comparison(*reports, expected=identity())["status"] == "pass"
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
        module.comparison(*reports, expected=identity())


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
    assert module.comparison(*reports, expected=identity())["status"] == "pass"
    cases = {row["scenario"]: row for row in reports[arm]["cases"]}
    native = cases[name]["runs"][attempt]["driver"]
    absent = cases[blocked]["runs"][0]["driver"]
    # Preserve run/trajectory identity and global session counts: only the
    # native-profile family correspondence is deliberately wrong.
    for field in ("native", "messages", "complete", "prompt_status", "failure_kind", "calls"):
        native[field], absent[field] = absent[field], native[field]
    with pytest.raises(ValueError):
        module.comparison(*reports, expected=identity())


@pytest.mark.parametrize("fault", [
    "stale-all-pins", "component-swap", "declared-state", "reducer-case", "reducer-calibration-id",
    "timing-calibration-case", "recovery-declaration", "native-config", "native-prompt",
    "native-trigger", "missing-run-id", "foreign-sources", "native-version", "population-digest", "native-final",
    "actual-arrival", "foreign-root-headlong", "foreign-root-derived", "foreign-root-fixtures", "foreign-root-genesis",
])
def test_consistently_rehashed_misbound_evidence_cannot_pass(module, reports, fault):
    from genesis.eval.qualification.evidence import digest

    b, c, recovery, timing = reports
    assert module.comparison(*reports, expected=identity())["status"] == "pass"
    if fault == "stale-all-pins":
        for report in reports:
            report["source_pin"] = "d" * 40
    elif fault == "component-swap":
        c["cases"][0]["manifest"]["sources"]["loop"] = "d" * 64
    elif fault == "declared-state":
        c["declared_population"]["state"]["outcomes"][0]["id"] = "foreign"
        c["state_controls"]["inventory"] = copy.deepcopy(c["declared_population"]["state"])
        c["state_controls"]["rows"]["outcomes"][0]["id"] = "foreign"
    elif fault == "reducer-case":
        c["state_controls"]["rows"]["outcomes"][0]["id"] = "foreign"
    elif fault == "reducer-calibration-id":
        c["state_controls"]["calibration"][0]["id"] = "foreign"
    elif fault == "timing-calibration-case":
        timing["calibration"][0]["case"] = "foreign"
    elif fault == "recovery-declaration":
        recovery["declared_population"]["crashes"][0] = "foreign"
        recovery["crashes"][0]["stage"] = "foreign"
    elif fault == "native-config":
        c["cases"][0]["runs"][0]["config"]["scenario"] = "noop"
    elif fault == "native-prompt":
        c["cases"][0]["runs"][0]["raw_prompt"] = "changed"
    elif fault == "native-trigger":
        c["cases"][0]["runs"][0]["config"]["trigger_id"] = "foreign"
    elif fault == "missing-run-id":
        b["cases"][0]["runs"][0]["driver"]["run_id"] = ""
        for record in b["cases"][0]["runs"][0]["trajectory"]:
            record.pop("run_id", None)
    elif fault == "foreign-sources":
        for report in reports:
            report["sources"] = {}
    elif fault.startswith("foreign-root-"):
        for report in reports:
            report["sources"][fault.removeprefix("foreign-root-")] = "e" * 64
    elif fault == "native-version":
        timing["native_binary"]["version"] = "foreign"
    elif fault == "native-final":
        run = c["cases"][0]["runs"][0]
        run["driver"]["native"]["session_id"] = str(uuid.uuid4())
        run["driver"]["messages"][1]["data"]["data"]["session_id"] = run["driver"]["native"]["session_id"]
        c["cases"][0]["reports"][-1]["observations"][0]["evidence"]["driver"] = copy.deepcopy(run["driver"])
    elif fault == "actual-arrival":
        row = next(row for row in timing["cases"] if row["scenario"] == "reactive-3")
        row["journal"][0]["data"]["observed_at"] = 104
        row["reports"][-1]["observations"][0]["observed_at"] = 104
        raw = "".join(json.dumps(record) + "\n" for record in row["journal"]).encode()
        row["artifacts"][0].update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest(), content_b64=base64.b64encode(raw).decode())
    else:
        c["population_sha256"] = "d" * 64
    if fault != "population-digest":
        for report in (c, recovery):
            report["population_sha256"] = digest(report["declared_population"])
    with pytest.raises(ValueError):
        module.comparison(*reports, expected=identity())
