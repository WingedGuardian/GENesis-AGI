"""Private comparison readback primitives; no runtime or approval authority."""

import hashlib
import json
import math
import re
import sys
import uuid
from pathlib import Path

from ego_runtime_cases import CASES, manifest


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


def contracts():
    # Producers import the numeric primitives above; defer these reverse imports.
    from ego_owned_matrix import population as native
    from ego_owned_recovery import population as recovery
    from ego_owned_state_controls import CALIBRATION_FIELDS as state_fields
    from ego_owned_state_controls import population as state
    from ego_owned_timing import CALIBRATION_FIELDS as timing_fields
    from ego_owned_timing import population as timing

    return native(), state(), recovery(), timing(), state_fields, timing_fields


def calibration(rows, keys, expected):
    found = [tuple(row[key] for key in keys) for row in rows]
    require(len(found) == len(set(found)) == len(expected) and set(found) == set(expected)
            and all(row['rejected'] is True for row in rows), 'incomplete calibration population')


def bound_manifest(row, expected):
    spec = row['manifest']
    require(spec['source_pin'] == expected['source_pin'] and equal(spec['sources'], expected['components'])
            and spec['executor'] == 'claude-code' and spec['model'] == 'sonnet', 'misbound source/config manifest')


def index(rows, key, names):
    require(isinstance(rows, list) and len(rows) == len(names), "incomplete report population")
    actual = [row[key] for row in rows]
    require(len(set(actual)) == len(actual) and set(actual) == set(names), "duplicate or mismatched report cases")
    require(all(row["passed"] is True for row in rows), "unqualified report case")
    return {row[key]: row for row in rows}


def native_profile(driver, outer):
    require(driver["outer_source"] == outer and driver["model"] == "sonnet" and type(driver["complete"]) is bool,
            "native driver profile mismatch")
    require(isinstance(driver['run_id'], str) and bool(driver['run_id']), 'missing native run identity')
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


def bound_records(driver, rows):
    headers = [row for row in rows if row.get('step_id') == driver['run_id']]
    require(len(headers) == 1 and headers[0]['type'] == 'shellm-run'
            and headers[0]['source'] == driver['outer_source']
            and headers[0]['invocation_id'] == driver['invocation_id']
            and headers[0]['trigger_step'] == driver['trigger_id'], 'misbound native run header')
    linked = [row for row in rows if row.get('run_id') == driver['run_id']]
    require(all(row['source'] == driver['outer_source'] for row in linked), 'misbound run source')
    finals = [row for row in linked if row['type'] == 'final']
    require(len(finals) == int(driver['complete']) and (not finals or
            finals[0]['native_session_id'] == driver['native']['session_id']), 'misbound native final')


def mechanics(b, c, expected_identity):
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
        bound_manifest(cr, expected_identity)
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
            evidence = observed[i]['evidence']
            if case.kind == 'handler_error' or case.fault == 'observation':
                require('driver' not in evidence and equal(evidence['errors'], ['ValueError: unfinished or failed protected handler'])
                        and cd['complete'] is False, 'missing expected protected-handler failure')
            else:
                require(equal(evidence['driver'], cd), 'misbound observed driver')
            config = cl['config']
            require(config['scenario'] == case.name and config['outer_source'] == 'ego-owned-loop'
                    and config['model'] == 'sonnet' and config['invocation_id'] == cd['invocation_id']
                    and config['trigger_id'] == cd['trigger_id']
                    and hashlib.sha256(cl['raw_prompt'].encode()).hexdigest() == cd['prompt_sha256'],
                    'misbound C invocation/config/prompt')
            profiles = {}
            for arm, driver, outer in (("B", bd, "monolith"), ("C", cd, "ego-owned-loop")):
                profile = native_profile(driver, outer)
                require((profile is None) == case.blocked, "native presence does not match fixed case")
                profiles[arm] = profile
                if profile is not None:
                    sessions[arm].append(profile["session_id"])
            bound_records(bd, bl['trajectory'])
            bound_records(cd, cr['trajectory'])
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
    count = sum(case.wakes for case in CASES if not case.blocked)
    for arm, values in sessions.items():
        require(len(values) == len(set(values)) == count, "native session denominator mismatch: " + arm)
    require(type(c["native_sessions"]) is int and c["native_sessions"] == count, "invalid declared C native sessions")
    return output


def state_population(c, declared, fields):
    state = c["state_controls"]
    expected = {group: len(declared[group]) for group in ('outcomes', 'bursts', 'guards', 'pacing')}
    count = sum(expected.values())
    require(state['schema'] == 'ego-owned-state-controls/v1' and equal(state['inventory'], declared)
            and state["status"] == "pass" and len(state["journals"]) == count, "incomplete reducer evidence")
    require(set(state["rows"]) == set(expected), "wrong reducer groups")
    for group in expected:
        rows = state["rows"][group]
        index(rows, 'id', [v['id'] if isinstance(v, dict) else v for v in declared[group]])
    mutations = [(cell['id'], field) for cell in declared['outcomes'] for field in fields]
    calibration(state['calibration'], ('id', 'mutant'), mutations)
    return {"arm": "C", "cells": sum(expected.values()), "groups": expected,
            "journals": len(state['journals']), "calibration_mutants": len(mutations)}


def recovery_population(report, declared, expected):
    crashes = index(report["crashes"], "stage", declared['crashes'])
    faults = index(report["late_io"], "phase", declared['late_io'])
    for row in [*crashes.values(), *faults.values()]:
        bound_manifest(row, expected)
    require(all(r["scopes_closed"] is True for r in list(crashes.values()) + list(faults.values())), "unclosed recovery scope")
    controls = report["calibration"]
    require(set(controls) == set(declared['calibration']), 'wrong recovery calibration')
    require(controls["init-adoption"]["passed"] is True and controls["init-adoption"]["missing_closure_rejected"] is True
            and controls["ordinary-effect"]["passed"] is True and controls["no-op-rejected"]["passed"] is False, "incomplete recovery controls")
    return {"arm": "C", "process_interruptions": len(crashes), "late_io_faults": len(faults), "calibration_controls": len(controls)}


def timing_population(report, declared, fields, expected):
    from ego_owned_timing import captured_journal, classify, observations

    names = ['timers', *[f'reactive-{n}' for n in declared['reactive_offsets']], *declared['lifecycle']]
    rows = index(report["cases"], "scenario", names)
    for row in rows.values():
        bound_manifest(row, expected)
        _, retained = captured_journal(row['artifacts'])
        require(equal(retained, row['journal']) and equal(observations(retained), row['reports'][-1]['observations']),
                'misbound timing journal')
    arrivals = [rows[f'reactive-{n}'] for n in declared['reactive_offsets']]
    require(all(row["classification"] in {"already-pending", "during-backoff", "later"}
                and type(row["offset"]) is int and row["offset"] == expected
                for row, expected in zip(arrivals, declared['reactive_offsets'], strict=True)), "invalid reactive arrival classification or offset")
    require(all(len(row['manifest']['feed']) == 1 and row['manifest']['feed'][0]['due'] == row['manifest']['start'] + row['offset']
                and row['classification'] == classify(row['offset'], row['manifest'], row['journal']) for row in arrivals),
            'misbound actual reactive arrival')
    require(all(row["scopes_closed"] is True for row in rows.values())
            and any(row["classification"] == "during-backoff" for row in arrivals), "incomplete timing applicability or closure")
    mutations = [(name, field) for name in names for field in fields]
    calibration(report['calibration'], ('case', 'corruption'), mutations)
    sessions = []
    for name, row in rows.items():
        observed = row["reports"][-1]["observations"]
        require(len(observed) == (declared['timer_wakes'] if name == "timers" else 3 if name.startswith("reactive-") else 1), "timing wake denominator mismatch")
        for observation in observed:
            driver = observation["evidence"]["driver"]
            profile = native_profile(driver, "ego-owned-loop")
            require(profile is not None and profile["is_error"] is False and driver["complete"] is True, "missing or failed timing native session")
            sessions.append(profile["session_id"])
    count = declared['timer_wakes'] + 3 * len(arrivals) + len(declared['lifecycle'])
    require(len(sessions) == len(set(sessions)) == count, "timing native session denominator mismatch")
    return {"arm": "C", "timer_wakes": declared['timer_wakes'], "timer_waits": declared['timer_waits'],
            "fixed_arrivals": len(arrivals), "lifecycle_cells": len(declared['lifecycle']),
            "native_sessions": count, "calibration_mutants": len(mutations),
            "arrivals": [{"case": row['scenario'], "offset": row["offset"], "classification": row["classification"]} for row in arrivals]}


def comparison(b, c, recovery, timing, *, expected):
    from genesis.eval.qualification.evidence import digest

    native, state, recovery_cases, timing_cases, state_fields, timing_fields = contracts()
    require(re.fullmatch(r'[0-9a-f]{40}', expected['source_pin']) is not None, 'invalid expected source pin')
    reports = [b, c, recovery, timing]
    for report, schema in zip(reports, ["headlong-native-monolith/v1", "ego-owned-matrix/v1", "ego-owned-recovery/v1", "ego-owned-timing/v1"], strict=True):
        require(report["schema"] == schema and report["status"] == "pass" and report["sources_unchanged"] is True, "incomplete input report")
        require(report['source_pin'] == expected['source_pin'] and report["sdk"] == expected['sdk']
                and report["native_binary"]["sha256"] == expected['native_sha256'], "source/native version/profile mismatch")
        require(equal(report['sources'], expected['roots']), 'misbound image source inventory')
        require(set(report['sources']) == {'headlong', 'derived', 'fixtures', 'genesis'}
                and all(isinstance(v, str) and re.fullmatch(r'[0-9a-f]{64}', v) for v in report['sources'].values())
                and isinstance(report['native_binary']['version'], str) and bool(report['native_binary']['version']),
                'invalid source/native inventory')
        require(equal(report["sources"], b["sources"]) and report["source_pin"] == b["source_pin"]
                and equal(report["native_binary"], b["native_binary"]), "mixed source or native identities")
    require(b["cleanup_confirmed"] is c["cleanup_confirmed"] is True
            and recovery["challenge"] is timing["challenge"] is None, "incomplete cleanup or challenge")
    for report, population in ((c, {'native': native, 'state': state}), (recovery, recovery_cases), (timing, timing_cases)):
        require(equal(report['declared_population'], population), 'producer population mismatch')
        if report is not timing:
            require(report['population_sha256'] == digest(population), 'population digest mismatch')
    attempts = sum(case.wakes for case in CASES)
    sessions = sum(case.wakes for case in CASES if not case.blocked)
    return {"schema": "ego-runtime-comparison/v1", "status": "pass", "source_pin": b["source_pin"], "sources": b["sources"],
            "sdk": b["sdk"], "native_binary": b["native_binary"], "executor": "claude-code", "requested_model": "sonnet",
            "mechanics": {"cases": mechanics(b, c, expected), "denominators": {"cases": len(CASES), "B_attempts": attempts,
                          "C_attempts": attempts, "B_native_sessions": sessions, "C_native_sessions": sessions}},
            "pure_state": state_population(c, state, state_fields),
            "recovery": recovery_population(recovery, recovery_cases, expected),
            "timing": timing_population(timing, timing_cases, timing_fields, expected),
            "scope": "Summary of separate offline scripted populations; independent retained-byte/process/container verification remains required.",
            "unqualified": ["real-model usefulness", "cost/performance generalization", "production activation", "full ego workflow", "Codex execution", "B/C recovery and actual-clock parity"],
            "differences": ["B records provider failures as run errors even when its outer shell exits zero; C retains a logical retry.",
                            "B may reset pacing after a visible partial effect; C stops uncertain when completion evidence is incomplete."],
            "codex_followup": "A separate native adapter must satisfy the same invocation, authority, artifact, continuity and lifecycle contract and run these populations; this report establishes no Codex parity."}


def main():
    """Pure stdin consumer inside the existing evaluation image; no native run."""
    try:
        # Validate input shape before importing optional producer dependencies.
        raw = sys.stdin.read()
        payload = json.loads(raw)
        require(isinstance(payload, dict) and set(payload) == {'B', 'C', 'recovery', 'timing'}, 'missing input reports')
        import importlib.metadata

        import claude_agent_sdk
        from ego_owned_loop import source_hashes, source_pin
        from headlong_controls import tree_hash

        from genesis.eval.qualification.evidence import load_json

        # Re-decode retained input with the canonical duplicate/nonfinite guard.
        payload = load_json(raw)
        cli = Path(claude_agent_sdk.__file__).parent / '_bundled/claude'
        roots = {"headlong": "/headlong", "derived": "/headlong-b", "fixtures": "/fixture", "genesis": "/genesis-src"}
        require(all(Path(root).is_dir() for root in roots.values()), 'missing image source root')
        expected = {'source_pin': source_pin(), 'components': source_hashes(),
                    'roots': {name: tree_hash(Path(root)) for name, root in roots.items()},
                    'sdk': importlib.metadata.version('claude-agent-sdk'), 'native_sha256': hashlib.sha256(cli.read_bytes()).hexdigest()}
        result = comparison(*(payload[key] for key in ('B', 'C', 'recovery', 'timing')), expected=expected)
    except (OSError, ValueError, KeyError, TypeError, IndexError, AttributeError, ImportError):
        print(json.dumps({'schema': 'ego-runtime-comparison/v1', 'status': 'incomplete', 'reason': 'input evidence could not be qualified'}))
        return 1
    print(json.dumps(result, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
