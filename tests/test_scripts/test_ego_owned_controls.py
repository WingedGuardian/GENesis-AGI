"""Oracle/no-op calibration for early C smoke scoring; no native host runs."""

import copy
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def controls(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/ci"))
    import ego_owned_controls

    return ego_owned_controls


def oracle(module, name):
    outcomes = {"timers": ["noop", "noop"], "restart_effect": ["completed", "refused"],
                "reactive": ["noop"], "blocked": ["blocked"]}[name]
    prompts = [" ".join(module.MARKERS) for _ in outcomes]
    reference = str(uuid.uuid4())
    if name == "restart_effect":
        prompts[1] += f" details {reference} fixture receipt proposal=proposal"
    observations, backends = [], []
    for i, outcome in enumerate(outcomes):
        calls = ([{"canonical_called": True, "claim": i == 0, "outcome": "executed" if i == 0 else "refused"}]
                 if name == "restart_effect" else [])
        kind = "approved" if name == "restart_effect" and i == 0 else "pending" if name == "restart_effect" else "echo"
        driver = {"native": None if name == "blocked" else {"session_id": str(uuid.uuid4()), "is_error": False,
                                                          "subtype": "success", "result": "native-final-" + kind},
                  "complete": name != "blocked", "outer_source": "ego-owned-loop",
                  "prompt_status": "65" if name == "blocked" else "0",
                  "failure_kind": "reference_conflict" if name == "blocked" else None,
                  "calls": calls, "observation_id": None, "final_id": None,
                  "prompt_sha256": module.owned.sha(prompts[i].encode())}
        observations.append({"outcome": outcome, "observed_at": 100 + i * 5,
                             "evidence": {"driver": driver, "calls": calls, "errors": [], "references": [reference]}})
        backends.append({"unexpected": [], "main_requests": 0 if name == "blocked" else 1,
                         "captures": [{"body": {"messages": [{"content": prompts[i] + " @mention-target.txt"}]}}]})
    if name == "blocked":
        prompts = [""]
    return {"reports": [{"observations": observations, "status": "blocked" if name == "blocked" else "budget",
                         "state": {"cursor": int(name == "reactive"), "timer_done": 0 if name in {"reactive", "blocked"} else 2,
                                   "timer_reserved": 0 if name == "reactive" else 1 if name == "blocked" else 2,
                                   "level": 0, "attempts": len(outcomes), "wake_at": 115}}],
            "backends": backends, "prompts": prompts, "groups_closed": True,
            "state": {"status": "executed" if name == "restart_effect" else "pending"},
            "commands": [{"returncode": 2 if name == "blocked" else 0, "timed_out": False}],
            "effect": module.native.EFFECT.hex() if name == "restart_effect" else None}


@pytest.mark.parametrize("name", ["timers", "restart_effect", "reactive", "blocked"])
def test_scoring_oracle_passes_and_noop_fails(controls, name):
    case = oracle(controls, name)
    assert controls.qualify(name, case) is True
    case["reports"][0]["observations"] = []
    assert controls.qualify(name, case) is False


@pytest.mark.parametrize("name", ["timers", "restart_effect", "reactive", "blocked"])
@pytest.mark.parametrize("defect", ["effect", "state", "cleanup", "cursor", "timer", "outcome", "timeout", "backend"])
def test_scorer_rejects_changed_observable(controls, name, defect):
    case = copy.deepcopy(oracle(controls, name))
    if defect == "effect":
        case["effect"] = "bad-effect"
    elif defect == "state":
        case["state"]["status"] = "unverified"
    elif defect == "cleanup":
        case["groups_closed"] = False
    elif defect in {"cursor", "timer"}:
        case["reports"][0]["state"]["cursor" if defect == "cursor" else "timer_done"] = 99
    elif defect == "outcome":
        case["reports"][0]["observations"][0]["outcome"] = "uncertain"
    elif defect == "timeout":
        case["commands"][0]["timed_out"] = True
    else:
        case["backends"][0]["unexpected"] = ["unexpected endpoint"]
    assert controls.qualify(name, case) is False


@pytest.mark.parametrize("defect", ["context", "mention", "tools", "native", "session", "pacing"])
def test_timer_scorer_requires_actual_request_and_pacing(controls, defect):
    case = oracle(controls, "timers")
    if defect == "context":
        case["backends"][0]["captures"][0]["body"]["messages"][0]["content"] = "missing context @mention-target.txt"
    elif defect == "mention":
        case["backends"][0]["captures"][0]["body"]["messages"][0]["content"] += controls.MENTION_CONTENT
    elif defect == "tools":
        case["backends"][0]["unexpected"] = ["protected schema missing"]
    elif defect == "native":
        case["reports"][0]["observations"][0]["evidence"]["driver"]["complete"] = False
    elif defect == "session":
        rows = case["reports"][0]["observations"]
        rows[1]["evidence"]["driver"]["native"]["session_id"] = rows[0]["evidence"]["driver"]["native"]["session_id"]
    else:
        case["reports"][0]["observations"][1]["observed_at"] = 101
    assert controls.qualify("timers", case) is False


@pytest.mark.parametrize("defect", ["status", "kind", "prompt", "calls", "final"])
def test_blocked_control_requires_the_expected_reference_conflict(controls, defect):
    case = oracle(controls, "blocked")
    driver = case["reports"][0]["observations"][0]["evidence"]["driver"]
    if defect == "status":
        driver["prompt_status"] = "1"
    elif defect == "kind":
        driver["failure_kind"] = "sanitizer_failure"
    elif defect == "prompt":
        case["prompts"] = ["unexpected retained text"]
    elif defect == "calls":
        driver["calls"] = [{"claim": True}]
    else:
        driver["final_id"] = str(uuid.uuid4())
    assert controls.qualify("blocked", case) is False


@pytest.mark.parametrize("field,value", [("is_error", True), ("subtype", "error"), ("result", "false success")])
def test_success_scorer_rejects_false_native_terminal(controls, field, value):
    case = oracle(controls, "timers")
    case["reports"][0]["observations"][0]["evidence"]["driver"]["native"][field] = value
    assert controls.qualify("timers", case) is False
