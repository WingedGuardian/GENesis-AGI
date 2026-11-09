"""Shared mechanics metadata is descriptive, never an approval oracle."""

import dataclasses
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def cases(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/ci"))
    import ego_runtime_cases

    return ego_runtime_cases


def test_frozen_b_population_and_kinds(cases):
    expected = {
        "effect": ("approved", 1), "noop": ("echo", 1), "refusal": ("pending", 1),
        "provider_error": ("provider_error", 1), "handler_error": ("handler_error", 1),
        "large_context": ("approved", 1), "reactive_noop": ("echo", 1),
        "restart_duplicate": ("approved", 2), "reference_continuity": ("approved", 2),
        "observation_failure": ("approved", 1), "final_failure": ("approved", 1),
        "reference_conflict": ("approved", 1), "reactive_reference_conflict": ("approved", 1),
    }
    manifest = cases.manifest()
    assert manifest["schema"] == "ego-runtime-mechanics/v1"
    rows = manifest["cases"]
    assert len(rows) == 13 and {row["name"] for row in rows} == set(expected)
    assert {row["name"]: (row["kind"], row["wakes"]) for row in rows} == expected
    assert sum(row["wakes"] for row in rows) == 15
    assert sum(row["wakes"] for row in rows if not row["blocked"]) == 13
    assert {row["name"] for row in rows if row["reactive"]} == {"reactive_noop", "reactive_reference_conflict"}
    assert {row["fault"] for row in rows if row["fault"]} == {"observation", "final"}
    for name in expected:
        assert cases.kind_for(name) == expected[name][0]
        assert cases.kind_for(name, second=True) == ("echo" if name in {"noop", "reactive_noop"} else "pending")


def test_manifest_is_fresh_and_case_is_immutable(cases):
    first = cases.manifest()
    first["cases"][0]["name"] = "changed"
    assert cases.manifest()["cases"][0]["name"] == "effect"
    with pytest.raises(dataclasses.FrozenInstanceError):
        cases.CASES[0].name = "changed"


def test_unknown_case_is_not_silently_approved(cases):
    with pytest.raises(ValueError, match="unknown mechanics case"):
        cases.case_for("unknown")


@pytest.mark.parametrize("value,expected", [({}, "monolith"), ({"outer_source": "monolith"}, "monolith"),
                                           ({"outer_source": "ego-owned-loop"}, "ego-owned-loop")])
def test_outer_source_has_explicit_closed_values(cases, value, expected):
    assert cases.outer_source(value) == expected


@pytest.mark.parametrize("value", [None, False, 0, "", "foreign", [], {}])
def test_invalid_outer_source_is_not_normalized(cases, value):
    with pytest.raises(ValueError, match="outer source"):
        cases.outer_source({"outer_source": value})


def test_shared_module_is_in_the_actual_image_context():
    workflow = (ROOT / ".github/workflows/headlong-controls.yml").read_text()
    dockerfile = (ROOT / "scripts/ci/headlong_controls.Dockerfile").read_text()
    assert "cp scripts/ci/ego_runtime_cases.py" in workflow
    assert "COPY ego_runtime_cases.py /fixture/" in dockerfile
