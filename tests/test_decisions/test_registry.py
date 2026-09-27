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
    body = _VALID.replace(
        "consumes: argmax",
        "consumes: threshold\n    threshold: 0.81\n    dead_band: 0.05\n    tie_rule: inclusive",
    )
    assert _spec(body)["memory_relationship"].threshold == pytest.approx(0.81)


@pytest.mark.parametrize("bad", ["0", "1", "1.5", "-0.2"])
def test_threshold_outside_the_open_unit_interval_is_rejected(bad):
    """Carries a valid band so the failure is the INTERVAL check, not a missing field."""
    body = _VALID.replace(
        "consumes: argmax",
        f"consumes: threshold\n    threshold: {bad}\n    dead_band: 0.01\n    tie_rule: inclusive",
    )
    with pytest.raises(RegistryError, match=r"threshold=.* (inside|must)"):
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


# ---------------------------------------------------------------------------
# Dead band. A decision whose score sits near its cut is not repeatable:
# an identical request re-sent to a pinned decision model flipped 159 of 858
# near-cut decisions at a 0.50 cut, while the aggregate action rate moved only
# a point because the flips cancel. So a thresholding site may not branch on a
# bare cut — it declares a band around it and abstains inside the band.
# ---------------------------------------------------------------------------

from genesis.decisions.types import Verdict  # noqa: E402

_THRESH = _VALID.replace(
    "consumes: argmax",
    "consumes: threshold\n    threshold: 0.6\n    dead_band: 0.1\n    tie_rule: inclusive",
)


def _gated(tie="inclusive", cut=0.6, band=0.1):
    return _spec(
        _VALID.replace(
            "consumes: argmax",
            f"consumes: threshold\n    threshold: {cut}\n    dead_band: {band}\n"
            f"    tie_rule: {tie}",
        )
    )["memory_relationship"]


def test_a_threshold_site_with_band_and_tie_rule_loads():
    s = _spec(_THRESH)["memory_relationship"]
    assert s.dead_band == pytest.approx(0.1)
    assert s.tie_rule == "inclusive"


@pytest.mark.parametrize("missing", ["dead_band", "tie_rule"])
def test_a_threshold_site_must_declare_band_and_tie_rule(missing):
    body = "\n".join(ln for ln in _THRESH.splitlines() if not ln.strip().startswith(missing))
    with pytest.raises(RegistryError, match=missing):
        _spec(body)


@pytest.mark.parametrize("key,val", [("dead_band", "0.1"), ("tie_rule", "inclusive")])
def test_band_fields_on_a_non_threshold_site_are_rejected(key, val):
    """A stray band means gating was expected but never declared."""
    with pytest.raises(RegistryError, match=key):
        _spec(_VALID.replace("consumes: argmax", f"consumes: argmax\n    {key}: {val}"))


@pytest.mark.parametrize(
    "cut,band",
    [(0.6, 0), (0.6, -0.1), (0.6, 0.4), (0.95, 0.1), (0.05, 0.1), (0.6, "true")],
)
def test_the_band_must_be_positive_and_stay_inside_the_unit_interval(cut, band):
    with pytest.raises(RegistryError, match="dead_band"):
        _gated(cut=cut, band=band)


@pytest.mark.parametrize("tie", ["gt", "maybe", "'  '", "true"])
def test_the_tie_rule_is_a_closed_set(tie):
    with pytest.raises(RegistryError, match="tie_rule"):
        _gated(tie=tie)


@pytest.mark.parametrize(
    "p,verdict",
    [
        (0.95, Verdict.ACT),
        (0.71, Verdict.ACT),
        (0.69, Verdict.ABSTAIN),  # inside the band, above the cut
        (0.60, Verdict.ABSTAIN),  # exactly on the cut is always inside the band
        (0.51, Verdict.ABSTAIN),
        (0.49, Verdict.DECLINE),
        (0.02, Verdict.DECLINE),
    ],
)
def test_gate_abstains_inside_the_band(p, verdict):
    assert _gated().gate(p, mode="calibrated") is verdict


def test_the_tie_rule_decides_scores_exactly_on_a_band_edge():
    """Two-decimal probabilities land exactly on edges; the rule is explicit."""
    inclusive, exclusive = _gated("inclusive"), _gated("exclusive")
    assert inclusive.gate(0.7, mode="calibrated") is Verdict.ACT
    assert inclusive.gate(0.5, mode="calibrated") is Verdict.DECLINE
    assert exclusive.gate(0.7, mode="calibrated") is Verdict.ABSTAIN
    assert exclusive.gate(0.5, mode="calibrated") is Verdict.ABSTAIN


@pytest.mark.parametrize("bad", [float("nan"), -0.01, 1.01, float("inf")])
def test_gate_refuses_a_score_that_is_not_a_probability(bad):
    with pytest.raises(ValueError):
        _gated().gate(bad, mode="calibrated")


def test_gate_is_only_for_thresholding_sites():
    with pytest.raises(ValueError):
        _spec(_VALID)["memory_relationship"].gate(0.9, mode="calibrated")


# ---------------------------------------------------------------------------
# Audit round 1 on the dead band: each test below closes a finding or a
# mutation that survived the original suite.
# ---------------------------------------------------------------------------

from decimal import Decimal  # noqa: E402
from fractions import Fraction  # noqa: E402

from genesis.decisions.types import MIN_DEAD_BAND, band_problem  # noqa: E402


@pytest.mark.parametrize("bad", [True, False, Decimal("0.7"), "0.7", None])
def test_gate_rejects_a_bool_or_non_real_score(bad):
    """A yes/no answer's VALUE passed instead of its probability used to skip
    the band entirely as 1.0 or 0.0."""
    with pytest.raises(ValueError):
        _gated().gate(bad, mode="calibrated")


def test_gate_converts_other_reals_to_float_so_verdicts_match():
    """Fraction(7,10) used to ACT under exclusive where the float 0.7 abstains."""
    ex = _gated("exclusive")
    assert ex.gate(Fraction(7, 10), mode="calibrated") is ex.gate(0.7, mode="calibrated")


@pytest.mark.parametrize("p,verdict", [(0.0, Verdict.DECLINE), (1.0, Verdict.ACT)])
def test_gate_accepts_the_closed_unit_interval(p, verdict):
    """A model returning exactly 1.00 must not crash the gate."""
    assert _gated().gate(p, mode="calibrated") is verdict


@pytest.mark.parametrize("mode", ["typed", "legacy", Mode.TYPED])
def test_gate_abstains_whenever_the_site_may_not_threshold(mode):
    """No site may branch on a probability outside Calibrated mode."""
    assert _gated().gate(0.99, mode=mode) is Verdict.ABSTAIN
    assert _gated().gate(0.01, mode=mode) is Verdict.ABSTAIN


@pytest.mark.parametrize(
    "cut,band,edge_hi,edge_lo",
    [(0.85, 0.07, 0.92, 0.78), (0.05, 0.01, 0.06, 0.04), (0.03, 0.01, 0.04, 0.02)],
)
def test_edges_hold_for_pairs_where_float_addition_is_inexact(cut, band, edge_hi, edge_lo):
    """0.85 + 0.07 is 0.9199999999999999 unrounded — an exact 0.92 must still be
    ON the edge, so the tie rule, not float noise, decides it."""
    inc, exc = _gated("inclusive", cut, band), _gated("exclusive", cut, band)
    assert inc.gate(edge_hi, mode="calibrated") is Verdict.ACT
    assert inc.gate(edge_lo, mode="calibrated") is Verdict.DECLINE
    assert exc.gate(edge_hi, mode="calibrated") is Verdict.ABSTAIN
    assert exc.gate(edge_lo, mode="calibrated") is Verdict.ABSTAIN


@pytest.mark.parametrize(
    "threshold,band,tie",
    [
        (0.6, -0.1, "inclusive"),
        (0.6, 0.0, "inclusive"),
        (0.6, 0.5, "inclusive"),
        (0.6, float("nan"), "inclusive"),
        (1.5, 0.1, "inclusive"),
        (0.6, 0.1, "sideways"),
        (0.6, None, "inclusive"),
    ],
)
def test_a_directly_constructed_spec_cannot_skip_band_validation(threshold, band, tie):
    """The loader used to be the only validator; gate() now runs the same checks."""
    spec = DecisionSpec(
        name="t",
        type=QuestionType.NOUL,
        instructions="q",
        consumes=C.THRESHOLD,
        fallback=Fallback("a", "b"),
        owner="o",
        threshold=threshold,
        dead_band=band,
        tie_rule=tie,
    )
    with pytest.raises(ValueError):
        spec.gate(0.6, mode="calibrated")


@pytest.mark.parametrize(
    "cut,band",
    [(0.5, MIN_DEAD_BAND / 10), (0.5, 0.4999999999), (0.1, 0.1), (0.9, 0.1)],
)
def test_bands_that_collapse_or_empty_a_side_are_rejected(cut, band):
    """A sub-resolution band collapses onto the cut; a band reaching 0 or 1
    leaves one side undecidable."""
    assert band_problem(cut, band, "inclusive") is not None
    with pytest.raises(RegistryError):
        _gated(cut=cut, band=band)


@pytest.mark.parametrize("bad", ["'0.6'", "true", "999999999999999999999" * 20])
def test_threshold_is_typed_as_strictly_as_the_band(bad):
    """A string threshold used to load while a string band was rejected; a
    huge integer used to raise OverflowError instead of RegistryError."""
    with pytest.raises(RegistryError):
        _gated(cut=bad)


@pytest.mark.parametrize("tie", ["Inclusive", "INCLUSIVE"])
def test_tie_rule_is_case_sensitive(tie):
    with pytest.raises(RegistryError, match="tie_rule"):
        _gated(tie=tie)


def test_tie_rule_surrounding_whitespace_is_stripped():
    assert _gated(tie="'  inclusive  '").tie_rule == "inclusive"


def test_a_yaml_bool_band_is_rejected_as_a_type_not_a_range():
    with pytest.raises(RegistryError, match="must be a number"):
        _gated(band="true")
