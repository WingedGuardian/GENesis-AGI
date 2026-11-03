"""Generated state scoring rejects false-success and typed-state aliases."""

import copy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def module(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/ci"))
    import ego_owned_state_controls

    return ego_owned_state_controls


def inputs():
    return "a" * 40, {k: "b" * 64 for k in ("state", "loop", "context", "bridge", "cases")}


def test_generated_population_and_oracle_have_no_missing_cells(module, tmp_path):
    r = module.sweep(tmp_path / "sweep", *inputs())
    assert r["status"] == "pass"
    assert {k: len(v) for k, v in r["rows"].items()} == {"outcomes": 36, "bursts": 17, "guards": 6, "pacing": 1}
    assert len(r["inventory"]["excluded"]) == 1 and len(r["calibration"]) == 144
    assert all(row["passed"] for group in r["rows"].values() for row in group)


@pytest.mark.parametrize("defect", ["duplicate", "missing", "renamed", "extra_group"])
def test_population_coverage_is_identity_bound(module, defect):
    inventory = module.population()
    rows = {k: [{"id": v["id"] if isinstance(v, dict) else v} for v in inventory[k]]
            for k in ("outcomes", "bursts", "guards", "pacing")}
    assert module.complete_population(inventory, rows) is True
    if defect == "duplicate":
        rows["outcomes"][0] = copy.deepcopy(rows["outcomes"][1])
    elif defect == "missing":
        rows["guards"].pop()
    elif defect == "renamed":
        rows["bursts"][0]["id"] = "different"
    else:
        rows["unknown"] = []
    assert module.complete_population(inventory, rows) is False


@pytest.mark.parametrize("field", ["cursor", "timer_done", "timer_reserved", "level", "attempts", "wake_at",
                                  "observations", "prepared_cursor", "bound_timer_done"])
def test_boolean_cannot_masquerade_as_candidate_numeric_state(module, tmp_path, field):
    cell = {"id": "typed-state", "kind": "reactive" if field == "wake_at" else "timer",
            "outcome": "completed" if field == "wake_at" else "noop", "delta": -100 if field == "wake_at" else 0}
    row = module.outcome(tmp_path / "case", *inputs(), cell)
    assert module.qualify_outcome(cell, row) is True
    if field == "observations":
        row[field] = True
    elif field == "prepared_cursor":
        row["prepared"]["cursor"] = False
    elif field == "bound_timer_done":
        row["bound"]["timer_done"] = False
    else:
        value = field not in {"cursor", "wake_at"}
        row["complete"][field] = value
        row["reopened"][field] = value
    assert module.qualify_outcome(cell, row) is False


@pytest.mark.parametrize("field", ["event", "at"])
def test_retry_observation_retains_exact_logical_work(module, tmp_path, field):
    cell = {"id": "retry-binding", "kind": "timer", "outcome": "retry", "delta": 0}
    row = module.outcome(tmp_path / "case", *inputs(), cell)
    assert module.qualify_outcome(cell, row) is True
    changed = copy.deepcopy(row)
    if field == "event":
        changed["complete"]["retry"]["event"]["id"] = "unissued"
    else:
        changed["complete"]["retry"]["at"] = 999
    changed["reopened"] = copy.deepcopy(changed["complete"])
    assert module.qualify_outcome(cell, changed) is False
