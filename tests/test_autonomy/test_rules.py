"""Tests for genesis.autonomy.rules — RuleEngine and rule evaluation."""

from __future__ import annotations

import textwrap

from genesis.autonomy.rules import RuleContext, RuleEngine
from genesis.autonomy.types import ActionClass, ApprovalDecision, ProtectionLevel

# ---------------------------------------------------------------------------
# RuleEngine: loading
# ---------------------------------------------------------------------------


class TestRuleEngineLoading:
    """Rules load from YAML correctly."""

    def test_loads_rules_from_yaml(self, tmp_path) -> None:
        rules_file = tmp_path / "rules.yaml"
        rules_file.write_text(textwrap.dedent("""\
            rules:
              - rule_id: test_rule
                condition:
                  action_class: reversible
                decision: act
                description: Test rule
        """))
        engine = RuleEngine(rules_path=rules_file)
        assert engine.rule_count == 1

    def test_missing_file_loads_zero_rules(self, tmp_path) -> None:
        engine = RuleEngine(rules_path=tmp_path / "missing.yaml")
        assert engine.rule_count == 0

    def test_malformed_yaml_loads_zero_rules(self, tmp_path) -> None:
        rules_file = tmp_path / "rules.yaml"
        rules_file.write_text("{{{{not valid yaml::::")
        engine = RuleEngine(rules_path=rules_file)
        assert engine.rule_count == 0

    def test_invalid_decision_skips_rule(self, tmp_path) -> None:
        rules_file = tmp_path / "rules.yaml"
        rules_file.write_text(textwrap.dedent("""\
            rules:
              - rule_id: bad_decision
                condition:
                  action_class: reversible
                decision: explode
        """))
        engine = RuleEngine(rules_path=rules_file)
        assert engine.rule_count == 0

    def test_reload_picks_up_changes(self, tmp_path) -> None:
        rules_file = tmp_path / "rules.yaml"
        rules_file.write_text("rules:\n  - rule_id: r1\n    decision: act\n")
        engine = RuleEngine(rules_path=rules_file)
        assert engine.rule_count == 1

        rules_file.write_text(
            "rules:\n"
            "  - rule_id: r1\n    decision: act\n"
            "  - rule_id: r2\n    decision: propose\n"
        )
        engine.reload()
        assert engine.rule_count == 2


# ---------------------------------------------------------------------------
# RuleEngine: evaluation
# ---------------------------------------------------------------------------


class TestRuleEngineEvaluation:
    """Rule matching and first-match-wins semantics."""

    def _make_engine(self, tmp_path, yaml_text: str) -> RuleEngine:
        rules_file = tmp_path / "rules.yaml"
        rules_file.write_text(yaml_text)
        return RuleEngine(rules_path=rules_file)

    def test_first_match_wins(self, tmp_path) -> None:
        engine = self._make_engine(tmp_path, textwrap.dedent("""\
            rules:
              - rule_id: first
                condition:
                  action_class: reversible
                decision: block
              - rule_id: second
                condition:
                  action_class: reversible
                decision: act
        """))
        result = engine.evaluate(RuleContext(action_class=ActionClass.REVERSIBLE))
        assert result.rule_id == "first"
        assert result.decision == ApprovalDecision.BLOCK

    def test_no_match_returns_default_act(self, tmp_path) -> None:
        engine = self._make_engine(tmp_path, textwrap.dedent("""\
            rules:
              - rule_id: only_irreversible
                condition:
                  action_class: irreversible
                decision: propose
        """))
        result = engine.evaluate(RuleContext(action_class=ActionClass.REVERSIBLE))
        assert result.rule_id == "_default"
        assert result.decision == ApprovalDecision.ACT

    def test_wildcard_condition_matches_anything(self, tmp_path) -> None:
        engine = self._make_engine(tmp_path, textwrap.dedent("""\
            rules:
              - rule_id: catch_all
                condition: {}
                decision: propose
        """))
        result = engine.evaluate(RuleContext(action_class=ActionClass.REVERSIBLE))
        assert result.rule_id == "catch_all"
        assert result.decision == ApprovalDecision.PROPOSE

    def test_action_class_matching(self, tmp_path) -> None:
        engine = self._make_engine(tmp_path, textwrap.dedent("""\
            rules:
              - rule_id: irrev
                condition:
                  action_class: irreversible
                decision: propose
              - rule_id: rev
                condition:
                  action_class: reversible
                decision: act
        """))
        assert engine.evaluate(
            RuleContext(action_class=ActionClass.IRREVERSIBLE)
        ).rule_id == "irrev"
        assert engine.evaluate(
            RuleContext(action_class=ActionClass.REVERSIBLE)
        ).rule_id == "rev"

    def test_protection_level_matching(self, tmp_path) -> None:
        engine = self._make_engine(tmp_path, textwrap.dedent("""\
            rules:
              - rule_id: crit
                condition:
                  protection_level: critical
                decision: block
        """))
        result = engine.evaluate(
            RuleContext(protection_level=ProtectionLevel.CRITICAL)
        )
        assert result.decision == ApprovalDecision.BLOCK
        # Normal doesn't match
        result = engine.evaluate(
            RuleContext(protection_level=ProtectionLevel.NORMAL)
        )
        assert result.decision == ApprovalDecision.ACT  # default

    def test_context_list_matching(self, tmp_path) -> None:
        engine = self._make_engine(tmp_path, textwrap.dedent("""\
            rules:
              - rule_id: relay_block
                condition:
                  context: [relay, background]
                decision: block
        """))
        assert engine.evaluate(
            RuleContext(context_category="relay")
        ).decision == ApprovalDecision.BLOCK
        assert engine.evaluate(
            RuleContext(context_category="background")
        ).decision == ApprovalDecision.BLOCK
        assert engine.evaluate(
            RuleContext(context_category="direct")
        ).decision == ApprovalDecision.ACT  # default

    def test_and_logic_all_must_match(self, tmp_path) -> None:
        engine = self._make_engine(tmp_path, textwrap.dedent("""\
            rules:
              - rule_id: compound
                condition:
                  protection_level: critical
                  context: [relay]
                decision: block
        """))
        # Both match
        assert engine.evaluate(RuleContext(
            protection_level=ProtectionLevel.CRITICAL,
            context_category="relay",
        )).decision == ApprovalDecision.BLOCK
        # Only one matches — no match
        assert engine.evaluate(RuleContext(
            protection_level=ProtectionLevel.CRITICAL,
            context_category="direct",
        )).decision == ApprovalDecision.ACT
        assert engine.evaluate(RuleContext(
            protection_level=ProtectionLevel.NORMAL,
            context_category="relay",
        )).decision == ApprovalDecision.ACT

    def test_timeout_returned(self, tmp_path) -> None:
        engine = self._make_engine(tmp_path, textwrap.dedent("""\
            rules:
              - rule_id: with_timeout
                condition:
                  action_class: costly_reversible
                decision: propose
                timeout_seconds: 3600
        """))
        result = engine.evaluate(
            RuleContext(action_class=ActionClass.COSTLY_REVERSIBLE)
        )
        assert result.timeout_seconds == 3600

    def test_null_timeout(self, tmp_path) -> None:
        engine = self._make_engine(tmp_path, textwrap.dedent("""\
            rules:
              - rule_id: forever
                condition:
                  action_class: irreversible
                decision: propose
                timeout_seconds: null
        """))
        result = engine.evaluate(
            RuleContext(action_class=ActionClass.IRREVERSIBLE)
        )
        assert result.timeout_seconds is None

    def test_missing_context_field_does_not_match(self, tmp_path) -> None:
        """If rule checks context but ctx has no context_category, no match."""
        engine = self._make_engine(tmp_path, textwrap.dedent("""\
            rules:
              - rule_id: needs_context
                condition:
                  context: [relay]
                decision: block
        """))
        result = engine.evaluate(RuleContext())  # no context_category
        assert result.decision == ApprovalDecision.ACT  # default

    def test_null_condition_treated_as_wildcard(self, tmp_path) -> None:
        """condition: null should match everything (treated as empty dict)."""
        engine = self._make_engine(tmp_path, textwrap.dedent("""\
            rules:
              - rule_id: null_cond
                condition: null
                decision: propose
        """))
        result = engine.evaluate(RuleContext(action_class=ActionClass.REVERSIBLE))
        assert result.rule_id == "null_cond"
        assert result.decision == ApprovalDecision.PROPOSE

    def test_null_rules_list_loads_zero(self, tmp_path) -> None:
        """rules: null should not crash, just load zero rules."""
        engine = self._make_engine(tmp_path, "rules: null\n")
        assert engine.rule_count == 0

    def test_unknown_condition_key_still_loads(self, tmp_path) -> None:
        """Unknown condition keys are ignored with a warning, rule still loads."""
        engine = self._make_engine(tmp_path, textwrap.dedent("""\
            rules:
              - rule_id: typo_rule
                condition:
                  action_classs: irreversible
                decision: propose
        """))
        assert engine.rule_count == 1


# ---------------------------------------------------------------------------
# Regression: ActionClassifier produces same results with RuleEngine
# ---------------------------------------------------------------------------


class TestClassifierRuleEngineRegression:
    """ActionClassifier with RuleEngine produces identical V3 decisions."""

    def test_reversible_act(self, tmp_path) -> None:
        from genesis.autonomy.classification import ActionClassifier
        c = ActionClassifier(config_path=tmp_path / "missing.yaml")
        assert c.classify(ActionClass.REVERSIBLE, 1) is ApprovalDecision.ACT

    def test_costly_reversible_propose(self, tmp_path) -> None:
        from genesis.autonomy.classification import ActionClassifier
        c = ActionClassifier(config_path=tmp_path / "missing.yaml")
        assert c.classify(ActionClass.COSTLY_REVERSIBLE, 1) is ApprovalDecision.PROPOSE

    def test_irreversible_propose(self, tmp_path) -> None:
        from genesis.autonomy.classification import ActionClassifier
        c = ActionClassifier(config_path=tmp_path / "missing.yaml")
        assert c.classify(ActionClass.IRREVERSIBLE, 1) is ApprovalDecision.PROPOSE

    def test_level_ignored_v3(self, tmp_path) -> None:
        from genesis.autonomy.classification import ActionClassifier
        c = ActionClassifier(config_path=tmp_path / "missing.yaml")
        for ac in ActionClass:
            assert c.classify(ac, 1) == c.classify(ac, 4), (
                f"Level should not matter in V3 for {ac!r}"
            )
