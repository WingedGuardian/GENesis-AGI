"""Private comparison readback primitives; no runtime or approval authority."""

import json
import math
import uuid

from ego_runtime_cases import CASES, manifest

NATIVE_SHA = "356733f185fa988e6bf3aa9a555269fdd167982a2e2a80d3c8a78037bb92ad58"  # pragma: allowlist secret -- verified public SDK binary SHA-256
TIMING = ("timers", "reactive-3", "reactive-5", "reactive-8", "sigterm-backoff", "exclusive-writer")
CRASHES = ("before_intake", "after_intake", "prepared", "trigger", "config", "bound", "native", "claimed", "effect", "receipt", "collected", "committed")
LATE_IO = ("db_close", "driver_open", "driver_short", "driver_close", "rename", "stdout", "stdout_flush")


def finite_number(value):
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def valid_c_counters(state):
    return (isinstance(state, dict)
            and all(type(state.get(k)) is int and state[k] >= 0
                    for k in ("cursor", "timer_reserved", "timer_done", "level", "attempts"))
            and finite_number(state.get("wake_at")))


def require(condition, message):
    if not condition:
        raise ValueError(message)


def equal(left, right):
    # Python equality alone aliases bool to int, including nested population metadata.
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def index(rows, key, names):
    require(isinstance(rows, list) and len(rows) == len(names), "incomplete report population")
    actual = [row[key] for row in rows]
    require(len(set(actual)) == len(actual) and set(actual) == set(names), "duplicate or mismatched report cases")
    require(all(row["passed"] is True for row in rows), "unqualified report case")
    return {row[key]: row for row in rows}


def native_profile(driver, outer):
    require(driver["outer_source"] == outer and driver["model"] == "sonnet" and type(driver["complete"]) is bool,
            "native driver profile mismatch")
    native = driver["native"]
    if native is None:
        require(driver["complete"] is False and driver["prompt_status"] == "65"
                and driver["failure_kind"] == "reference_conflict" and driver["calls"] == [], "unexplained missing native")
        return None
    terminals = [m["data"] for m in driver["messages"] if m["type"] == "ResultMessage"]
    inits = [m["data"]["data"] for m in driver["messages"] if m["type"] == "SystemMessage" and m["data"]["subtype"] == "init"]
    require(equal(terminals, [native]) and len(inits) == 1 and type(native["is_error"]) is bool, "misbound typed native result")
    require(isinstance(native["subtype"], str) and bool(native["subtype"])
            and (native["is_error"] is True or native["subtype"] == "success"), "invalid typed native subtype")
    init = inits[0]
    require(init["tools"] == ["mcp__fixture__claim_effect"] and init["session_id"] == native["session_id"]
            and isinstance(init["model"], str) and bool(init["model"]), "resolved native profile mismatch")
    require(str(uuid.UUID(native["session_id"])) == native["session_id"], "invalid native session ID")
    return {"session_id": native["session_id"], "resolved_model": init["model"], "is_error": native["is_error"]}


def mechanics(b, c):
    expected = [case.name for case in CASES]
    require(equal(b["comparison_manifest"], manifest())
            and equal(c["declared_population"]["native"]["comparison_manifest"], manifest()), "mechanics manifest mismatch")
    # Native reports historically use control_passed rather than passed.
    left = index([{**row, "passed": row["control_passed"]} for row in b["cases"]], "scenario", expected)
    right = index([{**row, "passed": row["control_passed"]} for row in c["cases"]], "scenario", expected)
    declared = c["declared_population"]["native"]["cases"]
    require(len(declared) == len(expected) and len({d["name"] for d in declared}) == len(expected)
            and {d["name"] for d in declared} == set(expected), "incomplete declared native population")
    declared = {d["name"]: d for d in declared}
    output, sessions = [], {"B": [], "C": []}
    for case in CASES:
        br, cr = left[case.name], right[case.name]
        require(len(br["runs"]) == len(cr["runs"]) == case.wakes, "wrong native attempt denominator")
        observed = cr["reports"][-1]["observations"]
        require(len(observed) == case.wakes and valid_c_counters(cr["reports"][-1]["state"]), "invalid C observations or counters")
        declaration = declared[case.name]
        require(type(declaration["attempts"]) is int and declaration["attempts"] == case.wakes
                and type(declaration["native_sessions"]) is int and declaration["native_sessions"] == (0 if case.blocked else case.wakes)
                and equal(declaration["outcomes"], [o["outcome"] for o in observed]), "native declaration does not match observed population")
        b_rows, c_rows = [], []
        for i, (bl, cl) in enumerate(zip(br["runs"], cr["runs"], strict=True)):
            bd, cd = bl["driver"], cl["driver"]
            profiles = {}
            for arm, driver, outer in (("B", bd, "monolith"), ("C", cd, "ego-owned-loop")):
                profile = native_profile(driver, outer)
                require((profile is None) == case.blocked, "native presence does not match fixed case")
                profiles[arm] = profile
                if profile is not None:
                    sessions[arm].append(profile["session_id"])
            require(type(bl["backoff"]["level"]) is int and bl["backoff"]["level"] >= 0
                    and finite_number(bl["wake_at"]) and type(bl["step_returncode"]) is int, "invalid B pacing or exit")
            effect = bl["effect"]
            require(effect in (None, "native-fixture-effect\n") and cr["after"]["effect"] in (None, b"native-fixture-effect\n".hex()),
                    "unexpected physical effect bytes")
            types = [row["type"] for row in bl["trajectory"] if row.get("run_id") == bd["run_id"]]
            require(all(t in {"error", "observation", "final", "idle"} for t in types), "unknown B run record")
            outcome = observed[i]["outcome"]
            require(outcome in {"completed", "noop", "refused", "retry", "uncertain", "blocked"}
                    and valid_c_counters(observed[i]["next"]), "unknown C outcome or pacing")
            b_rows.append({"driver_complete": bd["complete"], "run_records": types, "level": bl["backoff"]["level"],
                           "native_profile": profiles["B"],
                           "outer_shell_exit": bl["step_returncode"], "proposal_status": bl["state"]["status"],
                           "effect_bytes": len(effect.encode()) if effect else 0})
            c_rows.append({"driver_complete": cd["complete"], "outcome": outcome, "level": observed[i]["next"]["level"],
                           "native_profile": profiles["C"],
                           "cursor": observed[i]["next"]["cursor"], "timer_done": observed[i]["next"]["timer_done"]})
        output.append({"case": case.name, "B": b_rows, "C": c_rows,
                       "C_final_proposal_status": cr["after"]["state"]["status"],
                       "C_final_effect_bytes": len(bytes.fromhex(cr["after"]["effect"])) if cr["after"]["effect"] else 0})
    for arm, values in sessions.items():
        require(len(values) == len(set(values)) == 13, "native session denominator mismatch: " + arm)
    require(type(c["native_sessions"]) is int and c["native_sessions"] == 13, "invalid declared C native sessions")
    return output


def state_population(c):
    state = c["state_controls"]
    require(state["status"] == "pass" and len(state["journals"]) == 60, "incomplete reducer evidence")
    expected = {"outcomes": 36, "bursts": 17, "guards": 6, "pacing": 1}
    require(set(state["rows"]) == set(expected), "wrong reducer groups")
    for group, count in expected.items():
        rows = state["rows"][group]
        require(len(rows) == count and all(r["passed"] is True for r in rows)
                and len({r["id"] for r in rows}) == count, "incomplete reducer group")
    require(len(state["calibration"]) == 144 and all(r["rejected"] is True for r in state["calibration"]), "incomplete reducer mutations")
    return {"arm": "C", "cells": 60, "groups": expected, "journals": 60, "calibration_mutants": 144}


def recovery_population(report):
    crashes = index(report["crashes"], "stage", CRASHES)
    faults = index(report["late_io"], "phase", LATE_IO)
    require(all(r["scopes_closed"] is True for r in list(crashes.values()) + list(faults.values())), "unclosed recovery scope")
    controls = report["calibration"]
    require(controls["init-adoption"]["passed"] is True and controls["init-adoption"]["missing_closure_rejected"] is True
            and controls["ordinary-effect"]["passed"] is True and controls["no-op-rejected"]["passed"] is False, "incomplete recovery controls")
    return {"arm": "C", "process_interruptions": 12, "late_io_faults": 7, "calibration_controls": 3}


def timing_population(report):
    rows = index(report["cases"], "scenario", TIMING)
    require(equal(report["declared_population"]["timer_waits"], [5, 10, 20, 40, 40])
            and equal(report["declared_population"]["reactive_offsets"], [3, 5, 8]), "timing population mismatch")
    arrivals = [rows[name] for name in TIMING[1:4]]
    require(all(row["classification"] in {"already-pending", "during-backoff", "later"}
                and type(row["offset"]) is int and row["offset"] == expected
                for row, expected in zip(arrivals, [3, 5, 8], strict=True)), "invalid reactive arrival classification or offset")
    require(all(row["scopes_closed"] is True for row in rows.values())
            and any(row["classification"] == "during-backoff" for row in arrivals), "incomplete timing applicability or closure")
    require(len(report["calibration"]) == 24 and all(c["rejected"] is True for c in report["calibration"]), "incomplete timing mutations")
    sessions = []
    for name, row in rows.items():
        observed = row["reports"][-1]["observations"]
        require(len(observed) == (6 if name == "timers" else 3 if name.startswith("reactive-") else 1), "timing wake denominator mismatch")
        for observation in observed:
            driver = observation["evidence"]["driver"]
            profile = native_profile(driver, "ego-owned-loop")
            require(profile is not None and profile["is_error"] is False and driver["complete"] is True, "missing or failed timing native session")
            sessions.append(profile["session_id"])
    require(len(sessions) == len(set(sessions)) == 17, "timing native session denominator mismatch")
    return {"arm": "C", "timer_wakes": 6, "timer_waits": [5, 10, 20, 40, 40], "fixed_arrivals": 3, "lifecycle_cells": 2,
            "native_sessions": 17, "calibration_mutants": 24,
            "arrivals": [{"case": name, "offset": rows[name]["offset"], "classification": rows[name]["classification"]} for name in TIMING[1:4]]}


def comparison(b, c, recovery, timing):
    reports = [b, c, recovery, timing]
    for report, schema in zip(reports, ["headlong-native-monolith/v1", "ego-owned-matrix/v1", "ego-owned-recovery/v1", "ego-owned-timing/v1"], strict=True):
        require(report["schema"] == schema and report["status"] == "pass" and report["sources_unchanged"] is True, "incomplete input report")
        require(report["sdk"] == "0.2.172" and report["native_binary"]["sha256"] == NATIVE_SHA, "native version/profile mismatch")
        require(equal(report["sources"], b["sources"]) and report["source_pin"] == b["source_pin"]
                and equal(report["native_binary"], b["native_binary"]), "mixed source or native identities")
    require(b["cleanup_confirmed"] is c["cleanup_confirmed"] is True
            and recovery["challenge"] is timing["challenge"] is None, "incomplete cleanup or challenge")
    return {"schema": "ego-runtime-comparison/v1", "status": "pass", "source_pin": b["source_pin"], "sources": b["sources"],
            "sdk": b["sdk"], "native_binary": b["native_binary"], "executor": "claude-code", "requested_model": "sonnet",
            "mechanics": {"cases": mechanics(b, c), "denominators": {"cases": 13, "B_attempts": 15, "C_attempts": 15, "B_native_sessions": 13, "C_native_sessions": 13}},
            "pure_state": state_population(c), "recovery": recovery_population(recovery), "timing": timing_population(timing),
            "scope": "Summary of separate offline scripted populations; independent retained-byte/process/container verification remains required.",
            "unqualified": ["real-model usefulness", "cost/performance generalization", "production activation", "full ego workflow", "Codex execution", "B/C recovery and actual-clock parity"],
            "differences": ["B records provider failures as run errors even when its outer shell exits zero; C retains a logical retry.",
                            "B may reset pacing after a visible partial effect; C stops uncertain when completion evidence is incomplete."],
            "codex_followup": "A separate native adapter must satisfy the same invocation, authority, artifact, continuity and lifecycle contract and run these populations; this report establishes no Codex parity."}
