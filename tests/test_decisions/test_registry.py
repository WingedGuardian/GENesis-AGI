"""Decision registry validation.

The registry's value is not that it stores question specs — it is that the
specs are *checkable*. Every rule below exists because a measurement or a
ruling produced it:

- ``consumes: threshold`` requires a threshold, and a site may only threshold
  in Calibrated mode. Origin: an uncalibrated Laya score thresholded at an
  arbitrary 0.5 produced a total false negative (0/10) where the real signal
  sat at 0.08. A linter enforces what a reviewer would not notice.
- Every site declares ``fallback.typed`` and ``fallback.legacy``. A site that
  cannot say what it does without a model is not ready to be a decision site.
- A choice with more than ``CARDINALITY_SOFT_CAP`` options must declare a
  strategy. Origin: Banking77 measured 0.425 at 77 options against 0.870 for a
  model without the token-budget constraint.
"""

from __future__ import annotations

import pytest

from genesis.decisions.registry import RegistryError, load_registry_from_string
from genesis.decisions.types import CARDINALITY_SOFT_CAP, Consumes, QuestionType

_VALID = """
decisions:
  memory_relationship:
    type: choice
    instructions: "How does Note B relate to Note A?"
    options:
      duplicate: "B restates A with no new information"
      contradicts: "B asserts something incompatible with A"
      succeeded_by: "B updates or replaces A"
      distinct: "unrelated, or independently true"
    consumes: argmax
    latency_budget_ms: 2000
    fallback:
      typed: argmax_advisory
      legacy: existing_llm_path
    outcome_source: ledger_predictions
    owner: memory.relationship_classifier
"""


def _spec(body: str):
    return load_registry_from_string(body)


def test_valid_spec_parses_and_exposes_typed_fields():
    reg = _spec(_VALID)
    s = reg["memory_relationship"]
    assert s.type is QuestionType.CHOICE
    assert s.consumes is Consumes.ARGMAX
    assert list(s.options) == ["duplicate", "contradicts", "succeeded_by", "distinct"]
    assert s.cardinality == 4
    assert s.owner == "memory.relationship_classifier"


def test_threshold_consumer_without_a_threshold_is_rejected():
    """The linter rule — the reason `consumes` exists as a declared field."""
    body = _VALID.replace("consumes: argmax", "consumes: threshold")
    with pytest.raises(RegistryError, match="threshold"):
        _spec(body)


def test_threshold_consumer_with_a_threshold_is_accepted():
    body = _VALID.replace("consumes: argmax", "consumes: threshold\n    threshold: 0.81")
    assert _spec(body)["memory_relationship"].threshold == pytest.approx(0.81)


@pytest.mark.parametrize("bad", ["0", "1", "1.5", "-0.2"])
def test_threshold_outside_the_open_unit_interval_is_rejected(bad):
    body = _VALID.replace("consumes: argmax", f"consumes: threshold\n    threshold: {bad}")
    with pytest.raises(RegistryError, match="threshold"):
        _spec(body)


def test_threshold_on_a_non_threshold_consumer_is_rejected():
    """A stray threshold means the author expected gating they did not declare."""
    body = _VALID.replace("consumes: argmax", "consumes: argmax\n    threshold: 0.5")
    with pytest.raises(RegistryError, match="threshold"):
        _spec(body)


@pytest.mark.parametrize("missing", ["typed", "legacy"])
def test_every_site_must_declare_both_fallbacks(missing):
    body = _VALID.replace(f"      {missing}: ", f"      x_{missing}: ")
    with pytest.raises(RegistryError, match="fallback"):
        _spec(body)


def test_choice_requires_at_least_two_options():
    body = (
        _VALID.replace('      contradicts: "B asserts something incompatible with A"\n', "")
        .replace('      succeeded_by: "B updates or replaces A"\n', "")
        .replace('      distinct: "unrelated, or independently true"\n', "")
    )
    with pytest.raises(RegistryError, match="option"):
        _spec(body)


def test_noul_must_not_carry_options():
    body = _VALID.replace("type: choice", "type: noul")
    with pytest.raises(RegistryError, match="option"):
        _spec(body)


def test_high_cardinality_choice_must_declare_a_strategy():
    """Origin: Banking77 scored 0.425 at 77 options vs 0.870 without the constraint."""
    opts = "\n".join(
        f'      label_{i}: "option number {i}"' for i in range(CARDINALITY_SOFT_CAP + 1)
    )
    body = _VALID.replace(
        '      duplicate: "B restates A with no new information"\n'
        '      contradicts: "B asserts something incompatible with A"\n'
        '      succeeded_by: "B updates or replaces A"\n'
        '      distinct: "unrelated, or independently true"',
        opts,
    )
    with pytest.raises(RegistryError, match="cardinality"):
        _spec(body)

    ok = body.replace("consumes: argmax", "consumes: argmax\n    cardinality_strategy: shortlist")
    assert _spec(ok)["memory_relationship"].cardinality == CARDINALITY_SOFT_CAP + 1


def test_duplicate_owner_is_allowed_but_duplicate_id_is_not():
    body = _VALID + _VALID.split("decisions:")[1]
    with pytest.raises(RegistryError, match="duplicate"):
        _spec(body)


def test_shipped_registry_is_valid_and_non_empty():
    """The real config/decisions.yaml must satisfy every rule above."""
    from pathlib import Path

    from genesis.decisions.registry import load_registry

    root = Path(__file__).resolve().parents[2]
    reg = load_registry(root / "config" / "decisions.yaml")
    assert reg, "shipped registry is empty"
    for name, spec in reg.items():
        assert spec.owner, f"{name} declares no owner"
        assert spec.fallback.typed and spec.fallback.legacy


# ---------------------------------------------------------------------------
# Behavioural surface. These exist because an adversarial audit found that
# `usable_in`, `requires_calibration`, `Mode`, `criteria` and the local overlay
# had ZERO references in this file — which is precisely why a StrEnum identity
# bug inverted the gate while every validation test still passed.
# ---------------------------------------------------------------------------

import textwrap  # noqa: E402

from genesis.decisions.types import Consumes as C  # noqa: E402
from genesis.decisions.types import (  # noqa: E402
    DecisionSpec,
    Fallback,
    Mode,
)


def _bare(consumes) -> DecisionSpec:
    return DecisionSpec(
        name="t",
        type=QuestionType.NOUL,
        instructions="q",
        consumes=consumes,
        fallback=Fallback("a", "b"),
        owner="o",
    )


@pytest.mark.parametrize("consumes", ["threshold", C.THRESHOLD])
def test_requires_calibration_is_true_for_a_plain_string_too(consumes):
    """`is` against a StrEnum reported False for the string form, telling a
    thresholding site it needed no calibration."""
    assert _bare(consumes).requires_calibration is True


@pytest.mark.parametrize("consumes", ["argmax", C.ARGMAX, "ordering", C.ORDERING])
def test_requires_calibration_is_false_for_non_thresholding_consumers(consumes):
    assert _bare(consumes).requires_calibration is False


@pytest.mark.parametrize("mode", ["calibrated", Mode.CALIBRATED])
@pytest.mark.parametrize("consumes", ["argmax", "ordering", "threshold"])
def test_calibrated_mode_permits_every_consumer(mode, consumes):
    assert _bare(consumes).usable_in(mode) is True


@pytest.mark.parametrize("mode", ["legacy", Mode.LEGACY])
@pytest.mark.parametrize("consumes", ["argmax", "ordering", "threshold"])
def test_legacy_mode_permits_nothing(mode, consumes):
    """The string form returned True here — the inverted gate."""
    assert _bare(consumes).usable_in(mode) is False


@pytest.mark.parametrize("mode", ["typed", Mode.TYPED])
def test_typed_mode_blocks_only_the_thresholding_consumer(mode):
    assert _bare("argmax").usable_in(mode) is True
    assert _bare("ordering").usable_in(mode) is True
    assert _bare("threshold").usable_in(mode) is False


def test_an_unrecognised_mode_raises_rather_than_taking_the_permissive_branch():
    with pytest.raises(ValueError):
        _bare("threshold").usable_in("clibrated")


def test_cardinality_covers_all_three_primitives():
    assert _spec(_VALID)["memory_relationship"].cardinality == 4
    assert _bare("argmax").cardinality == 2
    score = _spec(
        textwrap.dedent("""
        decisions:
          s:
            type: score
            instructions: "how urgent?"
            criteria: ["low", "mid", "high"]
            consumes: ordering
            fallback: {typed: rank, legacy: heuristic}
            owner: o
        """)
    )["s"]
    assert score.cardinality == 3
    assert score.criteria == ("low", "mid", "high")


def test_options_are_copied_not_aliased():
    """MappingProxyType is a VIEW — wrapping a caller's live dict leaves them
    holding a handle that mutates a frozen instance."""
    live = {"a": "x", "b": "y"}
    spec = DecisionSpec(
        name="t",
        type=QuestionType.CHOICE,
        instructions="q",
        consumes=C.ARGMAX,
        fallback=Fallback("a", "b"),
        owner="o",
        options=live,
    )
    live["c"] = "z"
    assert "c" not in spec.options
    assert spec.cardinality == 2


def test_unknown_keys_are_rejected_so_a_typo_cannot_disable_a_gate():
    body = _VALID.replace("consumes: argmax", "consumes: argmax\n    thrshold: 0.9")
    with pytest.raises(RegistryError, match="unknown key"):
        _spec(body)


def test_cross_type_fields_are_rejected_not_silently_discarded():
    body = _VALID.replace("consumes: argmax", 'criteria: ["a", "b"]\n    consumes: argmax')
    with pytest.raises(RegistryError, match="criteria"):
        _spec(body)


def test_cardinality_soft_cap_applies_to_score_criteria_too():
    levels = ", ".join(f'"l{i}"' for i in range(CARDINALITY_SOFT_CAP + 1))
    body = textwrap.dedent(f"""
        decisions:
          s:
            type: score
            instructions: "how much?"
            criteria: [{levels}]
            consumes: ordering
            fallback: {{typed: rank, legacy: heuristic}}
            owner: o
        """)
    with pytest.raises(RegistryError, match="cardinality"):
        _spec(body)


@pytest.mark.parametrize("bad", ["banana", "true", "'  '"])
def test_cardinality_strategy_is_a_closed_set(bad):
    opts = "\n".join(f'      label_{i}: "opt {i}"' for i in range(CARDINALITY_SOFT_CAP + 1))
    body = _VALID.replace(
        '      duplicate: "B restates A with no new information"\n'
        '      contradicts: "B asserts something incompatible with A"\n'
        '      succeeded_by: "B updates or replaces A"\n'
        '      distinct: "unrelated, or independently true"',
        opts,
    ).replace("consumes: argmax", f"consumes: argmax\n    cardinality_strategy: {bad}")
    with pytest.raises(RegistryError):
        _spec(body)


@pytest.mark.parametrize("bad", ["true", "3.9", ".inf", "-1", "0", "'abc'"])
def test_latency_budget_rejects_every_non_positive_integer(bad):
    """bool is an int subclass and float('inf') raises OverflowError — both
    escaped the original int() coercion."""
    body = _VALID.replace("latency_budget_ms: 2000", f"latency_budget_ms: {bad}")
    with pytest.raises(RegistryError, match="latency_budget_ms"):
        _spec(body)


@pytest.mark.parametrize(
    "spelling",
    [
        '  "memory_relationship":\n    owner: other\n',
        "  memory_relationship:  # trailing comment\n    owner: o\n",
        "  memory_relationship: {owner: other}\n",  # flow style
    ],
)
def test_duplicate_ids_are_caught_in_every_yaml_spelling(spelling):
    """The hand-rolled two-space scan was a denylist; several valid-YAML
    spellings walked past it. Loader-level detection sees RESOLVED keys, so
    quoting, comments and flow style all collapse to the same check."""
    with pytest.raises(RegistryError, match="duplicate"):
        _spec(_VALID + spelling)


def test_duplicate_ids_are_caught_at_a_non_two_space_indent():
    """A whole registry written at 4-space indent — the case the hardcoded
    two-space scan could not see.

    Note this must be a genuine SIBLING duplicate: appending a 4-space-indented
    key to a 2-space document makes it a NESTED key of the previous decision,
    not a duplicate, which is a different defect the allowlist catches.
    """
    body = textwrap.dedent("""
        decisions:
            d:
                type: noul
                instructions: q
                consumes: argmax
                fallback:
                    typed: a
                    legacy: b
                owner: o
            d:
                type: noul
                instructions: q2
                consumes: argmax
                fallback:
                    typed: a
                    legacy: b
                owner: o2
        """)
    with pytest.raises(RegistryError, match="duplicate"):
        _spec(body)


def test_duplicate_detection_survives_a_space_before_the_colon():
    body = _VALID.replace("decisions:", "decisions :")
    with pytest.raises(RegistryError, match="duplicate"):
        _spec(body + "  memory_relationship:\n    owner: other\n")


def test_local_overlay_deep_merges_a_partial_override(tmp_path):
    """A replace-merge made an overlay supplying one field fail validation for
    every field it did not restate."""
    from genesis.decisions.registry import load_registry

    base = tmp_path / "decisions.yaml"
    base.write_text(_VALID)
    (tmp_path / "decisions.local.yaml").write_text(
        "decisions:\n  memory_relationship:\n    owner: local.override\n"
    )
    spec = load_registry(base)["memory_relationship"]
    assert spec.owner == "local.override"
    assert spec.cardinality == 4, "unrelated fields must survive the merge"


def test_a_duplicate_inside_the_overlay_is_caught(tmp_path):
    from genesis.decisions.registry import load_registry

    base = tmp_path / "decisions.yaml"
    base.write_text(_VALID)
    (tmp_path / "decisions.local.yaml").write_text(
        "decisions:\n  a:\n    owner: x\n  a:\n    owner: y\n"
    )
    with pytest.raises(RegistryError, match="duplicate"):
        load_registry(base)


def test_strict_loader_still_refuses_arbitrary_python_tags():
    """Subclassing SafeLoader must not re-enable object construction."""
    with pytest.raises(Exception) as exc:
        _spec('!!python/object/apply:os.system ["true"]')
    assert "python/object" in str(exc.value) or "constructor" in str(exc.value)
