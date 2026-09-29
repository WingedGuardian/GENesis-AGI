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
    # A thresholding spec is only constructible with its cut, band and tie rule.
    band = (
        {"threshold": 0.6, "dead_band": 0.1, "tie_rule": "inclusive"}
        if consumes in ("threshold", C.THRESHOLD)
        else {}
    )
    return DecisionSpec(
        name="t",
        type=QuestionType.NOUL,
        instructions="q",
        consumes=consumes,
        fallback=Fallback("a", "b"),
        owner="o",
        **band,
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
    with pytest.raises(RegistryError, match="latency_budget_ms|plain decimal"):
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


@pytest.fixture(autouse=True)
def _isolated_user_config(tmp_path, monkeypatch):
    """The overlay resolver checks ~/.genesis/config first. Point it at an empty
    per-test dir so a real install's overlay can never leak into a test."""
    user_dir = tmp_path / "user-config"
    user_dir.mkdir()
    monkeypatch.setattr("genesis._config_overlay._user_config_dir", lambda: user_dir)
    return user_dir


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
    assert _gated().gate(p, mode="calibrated", calibration_version="cal-v1") is verdict


def test_the_tie_rule_decides_scores_exactly_on_a_band_edge():
    """Two-decimal probabilities land exactly on edges; the rule is explicit."""
    inclusive, exclusive = _gated("inclusive"), _gated("exclusive")
    assert inclusive.gate(0.7, mode="calibrated", calibration_version="cal-v1") is Verdict.ACT
    assert inclusive.gate(0.5, mode="calibrated", calibration_version="cal-v1") is Verdict.DECLINE
    assert exclusive.gate(0.7, mode="calibrated", calibration_version="cal-v1") is Verdict.ABSTAIN
    assert exclusive.gate(0.5, mode="calibrated", calibration_version="cal-v1") is Verdict.ABSTAIN


@pytest.mark.parametrize("bad", [float("nan"), -0.01, 1.01, float("inf")])
def test_gate_refuses_a_score_that_is_not_a_probability(bad):
    with pytest.raises(ValueError):
        _gated().gate(bad, mode="calibrated", calibration_version="cal-v1")


def test_gate_is_only_for_thresholding_sites():
    with pytest.raises(ValueError):
        _spec(_VALID)["memory_relationship"].gate(
            0.9, mode="calibrated", calibration_version="cal-v1"
        )


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
        _gated().gate(bad, mode="calibrated", calibration_version="cal-v1")


def test_gate_converts_other_reals_to_float_so_verdicts_match():
    """Fraction(7,10) used to ACT under exclusive where the float 0.7 abstains."""
    ex = _gated("exclusive")
    assert ex.gate(Fraction(7, 10), mode="calibrated", calibration_version="cal-v1") is ex.gate(
        0.7, mode="calibrated", calibration_version="cal-v1"
    )


@pytest.mark.parametrize("p,verdict", [(0.0, Verdict.DECLINE), (1.0, Verdict.ACT)])
def test_gate_accepts_the_closed_unit_interval(p, verdict):
    """A model returning exactly 1.00 must not crash the gate."""
    assert _gated().gate(p, mode="calibrated", calibration_version="cal-v1") is verdict


@pytest.mark.parametrize("mode", ["typed", Mode.TYPED])
def test_gate_abstains_whenever_the_site_may_not_threshold(mode):
    """No site may branch on a probability outside Calibrated mode."""
    for version in (None, "cal-v1"):
        assert _gated().gate(0.99, mode=mode, calibration_version=version) is Verdict.ABSTAIN
        assert _gated().gate(0.01, mode=mode, calibration_version=version) is Verdict.ABSTAIN


@pytest.mark.parametrize("mode", ["legacy", Mode.LEGACY])
def test_legacy_mode_routes_to_the_legacy_fallback_not_the_typed_one(mode):
    """ABSTAIN means fallback.typed, which may need the backend Legacy lacks."""
    spec = _gated()
    for p in (0.99, 0.6, 0.01):
        verdict = spec.gate(p, mode=mode, calibration_version="cal-v1")
        assert verdict is Verdict.LEGACY
        assert spec.fallback_for(verdict) == spec.fallback.legacy
    assert spec.fallback_for(Verdict.ABSTAIN) == spec.fallback.typed
    assert spec.fallback.typed != spec.fallback.legacy, "fixture must tell them apart"


@pytest.mark.parametrize("version", [None, ""])
def test_calibrated_mode_without_a_calibration_version_may_not_branch(version):
    """A score nobody can trace to a calibration abstains, even far from the cut."""
    spec = _gated()
    assert spec.gate(0.99, mode="calibrated", calibration_version=version) is Verdict.ABSTAIN
    assert spec.gate(0.01, mode="calibrated", calibration_version=version) is Verdict.ABSTAIN
    assert spec.gate(0.99, mode="calibrated", calibration_version="cal-v1") is Verdict.ACT


@pytest.mark.parametrize("verdict", [Verdict.ACT, Verdict.DECLINE])
def test_fallback_for_refuses_a_deciding_verdict(verdict):
    with pytest.raises(ValueError, match="decision, not a fallback"):
        _gated().fallback_for(verdict)


@pytest.mark.parametrize("huge", [10**400, Fraction(10**400, 3)])
def test_an_unconvertible_score_is_a_value_error_not_an_overflow(huge):
    """float() of a huge int or Fraction raises OverflowError, which would
    escape a caller handling ValueError as 'invalid probability'."""
    with pytest.raises(ValueError, match="not a probability"):
        _gated().gate(huge, mode="calibrated", calibration_version="cal-v1")


@pytest.mark.parametrize(
    "cut,band,edge_hi,edge_lo",
    [(0.85, 0.07, 0.92, 0.78), (0.05, 0.01, 0.06, 0.04), (0.03, 0.01, 0.04, 0.02)],
)
def test_edges_hold_for_pairs_where_float_addition_is_inexact(cut, band, edge_hi, edge_lo):
    """0.85 + 0.07 is 0.9199999999999999 unrounded — an exact 0.92 must still be
    ON the edge, so the tie rule, not float noise, decides it."""
    inc, exc = _gated("inclusive", cut, band), _gated("exclusive", cut, band)
    assert inc.gate(edge_hi, mode="calibrated", calibration_version="cal-v1") is Verdict.ACT
    assert inc.gate(edge_lo, mode="calibrated", calibration_version="cal-v1") is Verdict.DECLINE
    assert exc.gate(edge_hi, mode="calibrated", calibration_version="cal-v1") is Verdict.ABSTAIN
    assert exc.gate(edge_lo, mode="calibrated", calibration_version="cal-v1") is Verdict.ABSTAIN


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
    """The loader used to be the only validator. The spec now validates itself
    on construction, so a bad band never reaches gate() at all."""
    with pytest.raises(ValueError):
        DecisionSpec(
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


def test_tie_rule_with_surrounding_whitespace_is_rejected_not_stripped():
    """Exact match, like every other closed-set field: quietly repairing input
    is how a value the author did not write gets loaded."""
    with pytest.raises(RegistryError, match="tie_rule"):
        _gated(tie="'  inclusive  '")


def test_a_yaml_bool_band_is_rejected_as_a_type_not_a_range():
    with pytest.raises(RegistryError, match="plain int or float"):
        _gated(band="true")


# ---------------------------------------------------------------------------
# Loader strictness: every shape below used to load clean while silently
# dropping or rewriting part of what the author declared.
# ---------------------------------------------------------------------------


def test_an_unknown_top_level_key_is_rejected():
    """`decision:` beside `decisions:` loaded clean and dropped its contents."""
    with pytest.raises(RegistryError, match="unknown top-level"):
        _spec(_VALID + "decision:\n  memory_relationship:\n    owner: lost\n")


@pytest.mark.parametrize("key", ["1", "yes", "no", "3.5", "null"])
def test_a_non_string_decision_name_is_rejected(key):
    """YAML reads these as int/bool/float/None; str() collided or rewrote them."""
    body = _VALID.replace("  memory_relationship:", f"  {key}:")
    with pytest.raises(RegistryError, match="valid string|match pattern"):
        _spec(body)


def test_a_quoted_numeric_decision_name_is_still_not_an_identifier():
    """Quoting makes it a string, but identifiers have a grammar, not just a type."""
    body = _VALID.replace("  memory_relationship:", '  "1":')
    with pytest.raises(RegistryError, match="match pattern"):
        _spec(body)


def test_int_and_string_keys_can_no_longer_collide():
    """`1` and `"1"` are distinct YAML keys; str() merged them, the later
    silently replacing the earlier despite the duplicate-key guard."""
    spec = _VALID.split("decisions:\n", 1)[1]
    body = (
        "decisions:\n"
        + spec.replace("  memory_relationship:", "  1:")
        + spec.replace("  memory_relationship:", '  "1":')
    )
    with pytest.raises(RegistryError, match="valid string|match pattern"):
        _spec(body)


@pytest.mark.parametrize("key", ["yes", "no", "1", "true"])
def test_a_non_string_option_key_is_rejected(key):
    """Bare `no:` became the label "False"."""
    body = _VALID.replace("      distinct:", f"      {key}:")
    with pytest.raises(RegistryError, match="valid string|match pattern"):
        _spec(body)


def test_an_unknown_fallback_key_is_rejected():
    body = _VALID.replace("      legacy: existing_llm_path", "      legacy: x\n      tyepd: y")
    with pytest.raises(RegistryError, match="Unexpected keyword argument"):
        _spec(body)


def test_a_misspelled_fallback_key_in_an_overlay_is_not_silently_ignored(tmp_path):
    """The typo merged beside the shipped `typed:`, so the base value won."""
    from genesis.decisions.registry import load_registry

    base = tmp_path / "decisions.yaml"
    base.write_text(_VALID)
    (tmp_path / "decisions.local.yaml").write_text(
        "decisions:\n  memory_relationship:\n    fallback:\n      tyepd: local_path\n"
    )
    with pytest.raises(RegistryError, match="Unexpected keyword argument"):
        load_registry(base)


def test_an_overlay_cannot_add_a_decision_the_base_does_not_ship(tmp_path):
    """A decision retired upstream must not survive in an old overlay."""
    from genesis.decisions.registry import load_registry

    retired = _VALID.split("decisions:\n", 1)[1].replace("memory_relationship", "retired_site")
    base = tmp_path / "decisions.yaml"
    base.write_text(_VALID)
    (tmp_path / "decisions.local.yaml").write_text("decisions:\n" + retired)
    with pytest.raises(RegistryError, match="retired_site"):
        load_registry(base)


def test_an_overlay_misspelled_top_level_key_is_rejected(tmp_path):
    from genesis.decisions.registry import load_registry

    base = tmp_path / "decisions.yaml"
    base.write_text(_VALID)
    (tmp_path / "decisions.local.yaml").write_text(
        "decision:\n  memory_relationship:\n    owner: lost\n"
    )
    with pytest.raises(RegistryError, match="unknown top-level"):
        load_registry(base)


@pytest.mark.parametrize("root", ["[]", "false", "0", '""'])
def test_a_falsy_non_mapping_overlay_is_an_error_not_no_overrides(tmp_path, root):
    """`or {}` turned these into "no overrides" without a word."""
    from genesis.decisions.registry import load_registry

    base = tmp_path / "decisions.yaml"
    base.write_text(_VALID)
    (tmp_path / "decisions.local.yaml").write_text(root + "\n")
    with pytest.raises(RegistryError, match="must be a mapping"):
        load_registry(base)


@pytest.mark.parametrize("empty", ["", "# nothing yet\n", "null\n"])
def test_an_empty_overlay_means_no_overrides(tmp_path, empty):
    from genesis.decisions.registry import load_registry

    base = tmp_path / "decisions.yaml"
    base.write_text(_VALID)
    (tmp_path / "decisions.local.yaml").write_text(empty)
    assert load_registry(base)["memory_relationship"].owner == "memory.relationship_classifier"


def test_the_overlay_is_found_in_the_user_config_dir_first(tmp_path, _isolated_user_config):
    """Settings writers put overlays in ~/.genesis/config; a sibling-only lookup
    ignored every override stored there."""
    from genesis.decisions.registry import load_registry

    base = tmp_path / "decisions.yaml"
    base.write_text(_VALID)
    (tmp_path / "decisions.local.yaml").write_text(
        "decisions:\n  memory_relationship:\n    owner: sibling\n"
    )
    (_isolated_user_config / "decisions.local.yaml").write_text(
        "decisions:\n  memory_relationship:\n    owner: user_dir\n"
    )
    assert load_registry(base)["memory_relationship"].owner == "user_dir"


def test_the_shipped_registry_loads():
    """The real config must satisfy every rule above."""
    from pathlib import Path

    from genesis.decisions.registry import load_registry

    root = Path(__file__).resolve().parents[2]
    reg = load_registry(root / "config" / "decisions.yaml")
    assert {"ego_proposal_reconcile", "ego_proposal_scope", "ego_proposal_realist"} <= set(reg)


# ---------------------------------------------------------------------------
# Round-1 audit: the value-side sibling of the non-string-key bug, plus gate
# ordering and provenance holes.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "old,new",
    [
        ("    owner: memory.relationship_classifier", "    owner: yes"),
        ('    instructions: "How does Note B relate to Note A?"', "    instructions: [a]"),
        ('      distinct: "unrelated, or independently true"', "      distinct: null"),
        ('      distinct: "unrelated, or independently true"', "      distinct: [1, 2]"),
        ("      typed: argmax_advisory", "      typed: yes"),
        ("      legacy: existing_llm_path", "      legacy: 1"),
        ("    outcome_source: ledger_predictions", "    outcome_source: yes"),
        ("    outcome_source: ledger_predictions", "    outcome_source: 0"),
        ("    outcome_source: ledger_predictions", "    outcome_source: false"),
    ],
)
def test_a_non_string_text_field_is_rejected_not_stringified(old, new):
    """str() turned `owner: yes` into "True" and `outcome_source: 0` into None."""
    assert old in _VALID
    with pytest.raises(RegistryError, match="valid string|at least 1 character"):
        _spec(_VALID.replace(old, new))


def test_an_absent_outcome_source_is_still_allowed():
    body = _VALID.replace("    outcome_source: ledger_predictions\n", "")
    assert _spec(body)["memory_relationship"].outcome_source is None


_SCORE = """
decisions:
  difficulty:
    type: score
    instructions: "How hard is this?"
    criteria: CRIT
    consumes: ordering
    fallback:
      typed: rank_advisory
      legacy: static
    owner: routing.router
"""


@pytest.mark.parametrize("crit", ["[yes, no]", '[1, "1"]', "[low, null]"])
def test_non_string_criteria_are_rejected(crit):
    with pytest.raises(RegistryError, match="valid string|at least 1 character"):
        _spec(_SCORE.replace("CRIT", crit))


def test_duplicate_criteria_levels_are_rejected():
    with pytest.raises(RegistryError, match="distinct levels"):
        _spec(_SCORE.replace("CRIT", "[low, low, high]"))


def test_distinct_string_criteria_are_accepted():
    assert _spec(_SCORE.replace("CRIT", "[low, high]"))["difficulty"].criteria == ("low", "high")


@pytest.mark.parametrize(
    "extra",
    [
        "    thrshold: 1\n    1: z\n",  # mixed str/int unknown spec keys
    ],
)
def test_mixed_type_unknown_keys_raise_registry_error_not_type_error(extra):
    """sorted() over {str, int} raised TypeError, escaping RegistryError handlers."""
    body = _VALID.replace("    consumes: argmax\n", "    consumes: argmax\n" + extra)
    with pytest.raises(RegistryError, match="unknown key"):
        _spec(body)


def test_mixed_type_unknown_fallback_keys_raise_registry_error():
    body = _VALID.replace(
        "      legacy: existing_llm_path",
        "      legacy: existing_llm_path\n      tyepd: q\n      1: z",
    )
    with pytest.raises(RegistryError, match="Unexpected keyword argument"):
        _spec(body)


def test_an_overlay_cannot_delete_an_option_by_nulling_it(tmp_path):
    """Deep-merge kept the key and str() made its description "None"."""
    from genesis.decisions.registry import load_registry

    base = tmp_path / "decisions.yaml"
    base.write_text(_VALID)
    (tmp_path / "decisions.local.yaml").write_text(
        "decisions:\n  memory_relationship:\n    options:\n      distinct: null\n"
    )
    with pytest.raises(RegistryError, match="options.distinct is null"):
        load_registry(base)


def test_an_overlay_nulling_decisions_says_so(tmp_path):
    from genesis.decisions.registry import load_registry

    base = tmp_path / "decisions.yaml"
    base.write_text(_VALID)
    (tmp_path / "decisions.local.yaml").write_text("decisions: null\n")
    with pytest.raises(RegistryError, match="decisions is null"):
        load_registry(base)


@pytest.mark.parametrize("p", [None, "n/a", float("nan"), 7])
def test_legacy_mode_needs_no_score(p):
    """No backend means no score; the caller must not have to invent one."""
    assert _gated().gate(p, mode="legacy") is Verdict.LEGACY


def test_a_bad_score_still_raises_outside_legacy_mode():
    with pytest.raises(ValueError, match="not a probability"):
        _gated().gate(None, mode="calibrated", calibration_version="cal-v1")


@pytest.mark.parametrize("version", [" ", "\t", 1, 1.0, True, b"cal-v1"])
def test_a_blank_or_non_string_calibration_version_does_not_count(version):
    """`not version` let " " and 1 through to ACT."""
    spec = _gated()
    assert spec.gate(0.99, mode="calibrated", calibration_version=version) is Verdict.ABSTAIN


# ---------------------------------------------------------------------------
# Round 2: closed by class, not by spelling.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "overlay",
    [
        "decisions:\n  memory_relationship:\n    outcome_source: null\n",
        "decisions:\n  memory_relationship:\n    latency_budget_ms: null\n",
        "decisions:\n  memory_relationship:\n    cardinality_strategy: null\n",
        "decisions:\n  memory_relationship:\n    options: null\n",
        "decisions:\n  memory_relationship:\n    fallback:\n      typed: null\n",
        "decisions:\n  memory_relationship: null\n",
        "decisions:\n  memory_relationship:\n    owner: x\n    outcome_source: ~\n",
    ],
)
def test_a_null_anywhere_in_an_overlay_is_rejected(tmp_path, overlay):
    """Deep-merge applied the null before validation, so it silently deleted a
    shipped constraint. One tree walk covers every field, present or future."""
    from genesis.decisions.registry import load_registry

    base = tmp_path / "decisions.yaml"
    base.write_text(_VALID)
    (tmp_path / "decisions.local.yaml").write_text(overlay)
    with pytest.raises(RegistryError, match="is null"):
        load_registry(base)


def test_a_null_free_overlay_still_merges(tmp_path):
    from genesis.decisions.registry import load_registry

    base = tmp_path / "decisions.yaml"
    base.write_text(_VALID)
    (tmp_path / "decisions.local.yaml").write_text(
        "decisions:\n  memory_relationship:\n    latency_budget_ms: 900\n"
    )
    spec = load_registry(base)["memory_relationship"]
    assert spec.latency_budget_ms == 900
    assert spec.outcome_source == "ledger_predictions"


@pytest.mark.parametrize("key", ['""', '" "', '"\\t"'])
def test_a_blank_decision_name_is_rejected(key):
    with pytest.raises(RegistryError, match="match pattern"):
        _spec(_VALID.replace("  memory_relationship:", f"  {key}:"))


@pytest.mark.parametrize("key", ['""', '"  "'])
def test_a_blank_option_label_is_rejected(key):
    with pytest.raises(RegistryError, match="match pattern"):
        _spec(_VALID.replace("      distinct:", f"      {key}:"))


@pytest.mark.parametrize("tie", ["[inclusive]", "{a: b}", "1", "yes"])
def test_a_non_string_tie_rule_is_a_registry_error(tie):
    """A YAML list is unhashable, so `in TIE_RULES` raised TypeError."""
    with pytest.raises(RegistryError, match="tie_rule"):
        _gated(tie=tie)


@pytest.mark.parametrize(
    "cut,band",
    [("0.5000000004", "0.001"), ("0.6", "0.0100000004"), ("0.1234567891", "0.01")],
)
def test_a_cut_or_band_finer_than_the_edge_rounding_is_rejected(cut, band):
    """Rounding the edge to 9 places moved a finer declared boundary: with a
    0.5000000004 cut, 0.5010000002 returned ACT from inside the true band."""
    with pytest.raises(RegistryError, match="decimal places"):
        _gated(cut=cut, band=band)


@pytest.mark.parametrize("cut,band", [("0.85", "0.07"), ("0.123456789", "0.001"), ("0.6", "0.1")])
def test_a_cut_and_band_within_nine_places_are_accepted(cut, band):
    assert _gated(cut=cut, band=band).threshold == pytest.approx(float(cut))


def test_the_registry_module_binds_no_overlay_seam_at_module_level():
    """A module-level alias holds its own reference that the test-suite's
    user-config isolation cannot reach (tests/test_config_overlay.py)."""
    import ast
    import inspect

    import genesis.decisions.registry as reg

    for node in ast.parse(inspect.getsource(reg)).body:
        if isinstance(node, ast.ImportFrom) and node.module == "genesis._config_overlay":
            raise AssertionError("module-level import from genesis._config_overlay")


import dataclasses  # noqa: E402

# ---------------------------------------------------------------------------
# Round 3: validation moved into one strict model behind one YAML entry point.
# Each block below is a CLASS the premise check found, one gap per layer.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "decisions:\n  ? [x]\n  : {}\n",  # unhashable key
        "decisions:\n  a: &s {owner: x}\n  b: *s\n",  # alias
        "base: &b {owner: x}\ndecisions:\n  a:\n    <<: *b\n",  # merge key via alias
        "decisions:\n  a:\n    <<: {owner: x}\n",  # merge key, inline
        "decisions:\n  a:\n    criteria: &c [x, *c]\n",  # recursive alias
        "decisions: {}\n---\ndecisions: {}\n",  # two documents
        "decisions: [unclosed\n",  # syntax error
    ],
)
def test_every_yaml_layer_failure_is_a_registry_error(text):
    """These raised TypeError, ConstructorError, RecursionError, ComposerError
    or ParserError, escaping any caller that handles RegistryError."""
    with pytest.raises(RegistryError):
        _spec(text)


@pytest.mark.parametrize("raw", ["0200", "1:30", "0x10", "1_000", "+0x1"])
def test_a_non_canonical_yaml_integer_is_rejected(raw):
    """YAML 1.1 read 0200 as 128 (octal) and 1:30 as 90 (base 60)."""
    body = _VALID.replace("latency_budget_ms: 2000", f"latency_budget_ms: {raw}")
    with pytest.raises(RegistryError, match="plain decimal"):
        _spec(body)


@pytest.mark.parametrize(
    "raw", ['"2000"', '"2_000"', '"٢٠٠٠"', '" 2000 "', "2000.0", "true", "0o17"]
)
def test_latency_budget_accepts_only_a_real_integer(raw):
    """int() turned '٢٠٠٠' (Arabic-Indic digits) and '2_000' into 2000."""
    body = _VALID.replace("latency_budget_ms: 2000", f"latency_budget_ms: {raw}")
    with pytest.raises(RegistryError, match="latency_budget_ms"):
        _spec(body)


def test_a_canonical_integer_still_loads():
    body = _VALID.replace("latency_budget_ms: 2000", "latency_budget_ms: 1500")
    assert _spec(body)["memory_relationship"].latency_budget_ms == 1500


@pytest.mark.parametrize("ident", ['" a"', '"a\\u00a0"', '"\\u200b"', '"\\ufeff"', '"A"', '"a-b"'])
def test_identifiers_have_a_grammar_not_just_a_type(ident):
    """A leading space or an invisible character made a key that looked right
    and could never be looked up."""
    with pytest.raises(RegistryError, match="match pattern"):
        _spec(_VALID.replace("  memory_relationship:", f"  {ident}:"))
    with pytest.raises(RegistryError, match="match pattern"):
        _spec(_VALID.replace("      distinct:", f"      {ident}:"))


@pytest.mark.parametrize("version", ["\u200b", "\ufeff", " cal", "cal v1", "-cal"])
def test_an_invisible_or_malformed_calibration_version_is_not_provenance(version):
    assert _gated().gate(0.99, mode="calibrated", calibration_version=version) is Verdict.ABSTAIN


@pytest.mark.parametrize("version", ["cal-v1", "2026.09.27", "site:abc+1", "v1_2"])
def test_a_well_formed_calibration_version_is_provenance(version):
    assert _gated().gate(0.99, mode="calibrated", calibration_version=version) is Verdict.ACT


def _direct(**over):
    kw = {
        "name": "t",
        "type": QuestionType.SCORE,
        "instructions": "q",
        "consumes": C.ORDERING,
        "fallback": Fallback("a", "b"),
        "owner": "o",
        "criteria": ("low", "high"),
    }
    kw.update(over)
    return DecisionSpec(**kw)


@pytest.mark.parametrize(
    "over",
    [
        {"criteria": "abc"},  # became ('a', 'b', 'c')
        {"criteria": {"x": 1, "y": 2}},  # became ('x', 'y')
        {"criteria": {"low", "high"}},  # a set has no order
        {"type": QuestionType.CHOICE, "criteria": (), "options": 5},
        {"type": QuestionType.CHOICE, "criteria": (), "options": None},
        {"fallback": None},
        {"name": None},
        {"owner": ""},
        {"type": QuestionType.NOUL, "criteria": (), "cardinality_strategy": "shortlist"},
    ],
)
def test_a_directly_constructed_spec_runs_every_rule(over):
    """Direct construction validated only the band; now it runs the whole model."""
    with pytest.raises(ValueError):
        _direct(**over)


@pytest.mark.parametrize("arms", [(None, None), ("", "b"), ("a", 1)])
def test_a_fallback_arm_must_be_an_identifier(arms):
    with pytest.raises(ValueError):
        Fallback(*arms)


def test_a_valid_direct_spec_still_constructs_and_is_frozen():
    spec = _direct()
    assert spec.criteria == ("low", "high")
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.name = "other"  # type: ignore[misc]


def test_an_overlay_cannot_add_an_option(tmp_path):
    from genesis.decisions.registry import load_registry

    base = tmp_path / "decisions.yaml"
    base.write_text(_VALID)
    (tmp_path / "decisions.local.yaml").write_text(
        "decisions:\n  memory_relationship:\n    options:\n      unrelated: extra\n"
    )
    with pytest.raises(RegistryError, match="adds option"):
        load_registry(base)


def test_an_overlay_may_reword_an_existing_option(tmp_path):
    from genesis.decisions.registry import load_registry

    base = tmp_path / "decisions.yaml"
    base.write_text(_VALID)
    (tmp_path / "decisions.local.yaml").write_text(
        "decisions:\n  memory_relationship:\n    options:\n      distinct: reworded\n"
    )
    spec = load_registry(base)["memory_relationship"]
    assert spec.options["distinct"] == "reworded"
    assert spec.cardinality == 4


def test_an_alias_is_rejected_even_when_it_would_resolve_to_a_valid_spec():
    """Isolates the alias rule: without it this file loads clean."""
    first = _VALID.replace(
        "    owner: memory.relationship_classifier", "    owner: &o memory.relationship_classifier"
    )
    second = _VALID.split("decisions:\n", 1)[1].replace("memory_relationship", "other_site")
    second = second.replace("    owner: memory.relationship_classifier", "    owner: *o")
    with pytest.raises(RegistryError, match="alias"):
        _spec(first + second)


def test_a_merge_key_is_rejected_with_its_own_message():
    """Isolates the merge-key rule from PyYAML's own constructor error."""
    body = _VALID.replace(
        "    fallback:\n      typed: argmax_advisory\n      legacy: existing_llm_path",
        "    fallback:\n      <<: {typed: argmax_advisory, legacy: existing_llm_path}",
    )
    with pytest.raises(RegistryError, match="merge key"):
        _spec(body)


# ---------------------------------------------------------------------------
# Round-3 audit.
# ---------------------------------------------------------------------------

_SCORE_BASE = """
decisions:
  difficulty:
    type: score
    instructions: "How hard is this?"
    criteria: [low, mid, high]
    consumes: ordering
    fallback:
      typed: rank_advisory
      legacy: static
    owner: routing.router
"""


@pytest.mark.parametrize(
    "override",
    [
        "    criteria: [high, mid, low]\n",  # reversed scale, same labels
        "    criteria: [low, mid, high, extreme]\n",  # extended scale
        "    type: choice\n",
        "    consumes: threshold\n    threshold: 0.6\n    dead_band: 0.1\n    tie_rule: inclusive\n",
    ],
)
def test_an_overlay_cannot_change_what_a_question_is(tmp_path, override):
    """Lists replace wholesale, so an overlay could invert a score's scale or
    promote an argmax site to threshold under the same name."""
    from genesis.decisions.registry import load_registry

    base = tmp_path / "decisions.yaml"
    base.write_text(_SCORE_BASE)
    (tmp_path / "decisions.local.yaml").write_text("decisions:\n  difficulty:\n" + override)
    with pytest.raises(RegistryError, match="what the question IS"):
        load_registry(base)


def test_an_overlay_may_still_retune_a_question(tmp_path):
    from genesis.decisions.registry import load_registry

    base = tmp_path / "decisions.yaml"
    base.write_text(_SCORE_BASE)
    (tmp_path / "decisions.local.yaml").write_text(
        'decisions:\n  difficulty:\n    instructions: "How demanding is this?"\n'
        "    latency_budget_ms: 700\n"
    )
    spec = load_registry(base)["difficulty"]
    assert spec.instructions == "How demanding is this?"
    assert spec.criteria == ("low", "mid", "high")


@pytest.mark.parametrize("raw", ["0:0.5", "0.0_5", "1_0.5", ".5", "5."])
def test_a_non_canonical_yaml_float_is_rejected(raw):
    """YAML 1.1 read 0:0.5 as 0.5 (base 60) and 0.0_5 as 0.05."""
    with pytest.raises(RegistryError, match="plain decimal|plain int or float|valid"):
        _gated(band=raw)


@pytest.mark.parametrize("raw", ["0.05", "0.1", "1.0e-2"])
def test_a_canonical_float_still_loads(raw):
    assert _gated(band=raw).dead_band == pytest.approx(float(raw))


@pytest.mark.parametrize("value", [Fraction(1, 2), Decimal("0.5"), True])
def test_a_direct_threshold_must_be_a_plain_number(value):
    """Fraction and Decimal were silently converted to float by the union type."""
    with pytest.raises(ValueError, match="plain int or float"):
        DecisionSpec(
            "t",
            QuestionType.NOUL,
            "q",
            C.THRESHOLD,
            Fallback("a", "b"),
            "o",
            threshold=value,
            dead_band=0.1,
            tie_rule="inclusive",
        )


def test_an_undecodable_overlay_is_a_registry_error(tmp_path):
    from genesis.decisions.registry import load_registry

    base = tmp_path / "decisions.yaml"
    base.write_text(_VALID)
    (tmp_path / "decisions.local.yaml").write_bytes(b"\xff\xfe\x00bad")
    with pytest.raises(RegistryError, match="cannot be read"):
        load_registry(base)


def test_a_yaml_boolean_error_says_to_quote_it():
    with pytest.raises(RegistryError, match="quote it"):
        _spec(_VALID.replace("    owner: memory.relationship_classifier", "    owner: yes"))


def test_the_shipped_volatility_site_declares_no_outcome_source():
    """Supersession is not a volatility label; declaring it would have label
    extraction mine a signal measured at chance."""
    from pathlib import Path

    from genesis.decisions.registry import load_registry

    root = Path(__file__).resolve().parents[2]
    assert (
        load_registry(root / "config" / "decisions.yaml")["memory_volatility"].outcome_source
        is None
    )


# ---------------------------------------------------------------------------
# Round-3 review.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "shipped",
    [
        _VALID.replace(
            '    options:\n      duplicate: "B restates A with no new information"\n'
            '      contradicts: "B asserts something incompatible with A"\n'
            '      succeeded_by: "B updates or replaces A"\n'
            '      distinct: "unrelated, or independently true"\n',
            "",
        ),  # a choice with no options
        "decisions:\n",  # decisions is null
        "decisions: []\n",
    ],
)
def test_an_overlay_cannot_mask_an_invalid_shipped_registry(tmp_path, shipped):
    """The shipped file was only checked after merging, so an overlay could
    supply what it lacked and it loaded on that one install alone."""
    from genesis.decisions.registry import load_registry

    base = tmp_path / "decisions.yaml"
    base.write_text(shipped)
    # A COMPLETE spec in the overlay: before the fix the merge alone was
    # validated, so this loaded for all three shipped files. Only a shipped
    # file validated on its own rejects every one of them.
    (tmp_path / "decisions.local.yaml").write_text(
        "decisions:\n  memory_relationship:\n    type: choice\n    instructions: q\n"
        "    consumes: argmax\n    fallback: {typed: a, legacy: b}\n    owner: o\n"
        "    options: {a: x, b: y}\n"
    )
    with pytest.raises(RegistryError, match="shipped, before any overlay"):
        load_registry(base)


def test_an_integer_beyond_the_digit_limit_is_a_registry_error():
    """int() of a 5000-digit token raises a bare ValueError (Python's
    int-string conversion limit), escaping the RegistryError contract."""
    body = _VALID.replace("latency_budget_ms: 2000", "latency_budget_ms: " + "9" * 5000)
    with pytest.raises(RegistryError, match="more than 18 digits"):
        _spec(body)


def test_the_integer_bound_does_not_depend_on_the_interpreter_setting():
    """Python's own digit limit can be switched off; the registry's bound cannot."""
    import sys

    old = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(0)  # 0 = unlimited
    try:
        body = _VALID.replace("latency_budget_ms: 2000", "latency_budget_ms: " + "9" * 19)
        with pytest.raises(RegistryError, match="more than 18 digits"):
            _spec(body)
    finally:
        sys.set_int_max_str_digits(old)


def test_no_shipped_site_declares_an_outcome_source_it_does_not_have():
    """Declared sources pointed at tables that do not exist or hold other labels."""
    from pathlib import Path

    from genesis.decisions.registry import load_registry

    root = Path(__file__).resolve().parents[2]
    reg = load_registry(root / "config" / "decisions.yaml")
    assert all(spec.outcome_source is None for spec in reg.values())
