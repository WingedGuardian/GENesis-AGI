"""Tests for genesis.autonomy.classification — ActionClassifier and classify_action()."""

from __future__ import annotations

from genesis.autonomy.classification import (
    ActionClassifier,
    classify_action,
    classify_domain,
)
from genesis.autonomy.types import ActionClass, ActionDomain, ApprovalDecision

# ---------------------------------------------------------------------------
# TestActionClassifier
# ---------------------------------------------------------------------------


class TestActionClassifier:
    """Tests for ActionClassifier.classify() and is_approval_required()."""

    def _make_classifier(self) -> ActionClassifier:
        """Build a classifier with no config file (V3 defaults)."""
        return ActionClassifier(config_path=None)

    def test_reversible_returns_act(self) -> None:
        c = self._make_classifier()
        assert c.classify(ActionClass.REVERSIBLE, 1) is ApprovalDecision.ACT

    def test_costly_reversible_returns_propose(self) -> None:
        c = self._make_classifier()
        assert c.classify(ActionClass.COSTLY_REVERSIBLE, 1) is ApprovalDecision.PROPOSE

    def test_irreversible_returns_propose(self) -> None:
        c = self._make_classifier()
        assert c.classify(ActionClass.IRREVERSIBLE, 1) is ApprovalDecision.PROPOSE

    def test_level_ignored_in_v3(self) -> None:
        """Same result at L1 and L4 for every action class — V3 ignores level."""
        c = self._make_classifier()
        for ac in ActionClass:
            assert c.classify(ac, 1) == c.classify(ac, 4), (
                f"Level should not matter in V3 for {ac!r}"
            )

    def test_is_approval_required_reversible(self) -> None:
        c = self._make_classifier()
        assert c.is_approval_required(ActionClass.REVERSIBLE, 1) is False

    def test_is_approval_required_costly(self) -> None:
        c = self._make_classifier()
        assert c.is_approval_required(ActionClass.COSTLY_REVERSIBLE, 1) is True

    def test_is_approval_required_irreversible(self) -> None:
        c = self._make_classifier()
        assert c.is_approval_required(ActionClass.IRREVERSIBLE, 1) is True


# ---------------------------------------------------------------------------
# TestGetTimeout
# ---------------------------------------------------------------------------


class TestGetTimeout:
    """Tests for ActionClassifier.get_timeout()."""

    def _make_classifier(self) -> ActionClassifier:
        return ActionClassifier(config_path=None)

    def test_outreach_timeout(self) -> None:
        c = self._make_classifier()
        assert c.get_timeout("outreach") is None

    def test_task_proposal_timeout(self) -> None:
        c = self._make_classifier()
        assert c.get_timeout("task_proposal") is None

    def test_irreversible_timeout_none(self) -> None:
        c = self._make_classifier()
        assert c.get_timeout("irreversible") is None

    def test_unknown_action_type(self) -> None:
        c = self._make_classifier()
        assert c.get_timeout("totally_unknown") is None

    def test_autonomous_cli_fallback_timeout(self) -> None:
        c = self._make_classifier()
        assert c.get_timeout("autonomous_cli_fallback") is None

    def test_sentinel_dispatch_timeout(self) -> None:
        c = self._make_classifier()
        assert c.get_timeout("sentinel_dispatch") is None

    def test_build_greenlight_timeout_none(self) -> None:
        # Greenlight cards wait indefinitely for a human tap.
        c = self._make_classifier()
        assert c.get_timeout("build_greenlight") is None

    def test_sentinel_action_timeout(self) -> None:
        c = self._make_classifier()
        assert c.get_timeout("sentinel_action") is None


# ---------------------------------------------------------------------------
# TestConfigLoading
# ---------------------------------------------------------------------------


class TestConfigLoading:
    """Tests for YAML config loading and fallback behavior."""

    def test_loads_from_yaml(self, tmp_path) -> None:
        cfg = tmp_path / "autonomy.yaml"
        cfg.write_text(
            "approval_policy:\n"
            "  reversible: act\n"
            "  costly_reversible: act\n"
            "  irreversible: propose\n"
            "approval_timeouts:\n"
            "  outreach: 7200\n"
        )
        # Use missing rules_path to isolate config-only behavior (fallback path)
        c = ActionClassifier(config_path=cfg, rules_path=tmp_path / "no_rules.yaml")
        # Custom policy: costly_reversible now maps to ACT
        assert c.classify(ActionClass.COSTLY_REVERSIBLE, 1) is ApprovalDecision.ACT
        # Custom timeout loaded
        assert c.get_timeout("outreach") == 7200

    def test_missing_config_uses_defaults(self, tmp_path) -> None:
        missing = tmp_path / "does_not_exist.yaml"
        c = ActionClassifier(config_path=missing, rules_path=tmp_path / "no_rules.yaml")
        # Should still produce V3 defaults
        assert c.classify(ActionClass.REVERSIBLE, 1) is ApprovalDecision.ACT
        assert c.classify(ActionClass.COSTLY_REVERSIBLE, 1) is ApprovalDecision.PROPOSE
        assert c.get_timeout("outreach") is None

    def test_malformed_yaml_uses_defaults(self, tmp_path) -> None:
        cfg = tmp_path / "autonomy.yaml"
        cfg.write_text("{{{{not valid yaml at all::::")
        c = ActionClassifier(config_path=cfg, rules_path=tmp_path / "no_rules.yaml")
        # Falls back to V3 defaults
        assert c.classify(ActionClass.IRREVERSIBLE, 1) is ApprovalDecision.PROPOSE
        assert c.get_timeout("task_proposal") is None


# ---------------------------------------------------------------------------
# TestClassifyAction (standalone function)
# ---------------------------------------------------------------------------


class TestClassifyAction:
    """Tests for the classify_action() keyword-matching hint function."""

    def test_send_message_costly(self) -> None:
        assert classify_action("send a message") is ActionClass.COSTLY_REVERSIBLE

    def test_push_code_costly(self) -> None:
        assert classify_action("push code to remote") is ActionClass.COSTLY_REVERSIBLE

    def test_delete_irreversible(self) -> None:
        assert classify_action("delete the user account") is ActionClass.IRREVERSIBLE

    def test_pay_irreversible(self) -> None:
        assert classify_action("pay the invoice") is ActionClass.IRREVERSIBLE

    def test_edit_file_reversible(self) -> None:
        assert classify_action("edit the config file") is ActionClass.REVERSIBLE

    def test_unknown_action_reversible(self) -> None:
        assert classify_action("do some analysis") is ActionClass.REVERSIBLE


# ---------------------------------------------------------------------------
# TestClassifyDomain (action_type -> ActionDomain)
# ---------------------------------------------------------------------------


class TestClassifyDomain:
    """Tests for classify_domain() — the action_type -> ActionDomain mapping
    that the dispatch gate uses to enforce autonomy levels."""

    def test_cognitive_variant_promotion_is_self_modify(self) -> None:
        """A reflection-prompt promotion changes Genesis's own cognition, so it
        must classify as SELF_MODIFY (hard-blocked from background dispatch) —
        NOT fall through to the EXTERNAL_READ default (always allowed)."""
        assert (
            classify_domain("cognitive_variant_promotion")
            is ActionDomain.SELF_MODIFY
        )

    def test_existing_code_change_self_modify(self) -> None:
        assert classify_domain("code_change") is ActionDomain.SELF_MODIFY

    def test_autonomous_build_maps_to_autonomous_build(self) -> None:
        """Build-lane dispatches classify as AUTONOMOUS_BUILD (min level 2,
        high-risk under a degraded gate) — NOT SELF_MODIFY (hard-blocked) and
        NOT the EXTERNAL_READ fallback (always allowed)."""
        assert (
            classify_domain("autonomous_build")
            is ActionDomain.AUTONOMOUS_BUILD
        )

    def test_self_modify_still_hard_blocked(self) -> None:
        """Adding AUTONOMOUS_BUILD must not soften SELF_MODIFY: its minimum
        level stays None (blocked from background dispatch entirely)."""
        from genesis.autonomy.types import ACTION_DOMAIN_MIN_LEVEL

        assert ACTION_DOMAIN_MIN_LEVEL[ActionDomain.SELF_MODIFY] is None
        assert ACTION_DOMAIN_MIN_LEVEL[ActionDomain.AUTONOMOUS_BUILD] == 2

    def test_unknown_action_type_falls_to_external_read(self) -> None:
        assert classify_domain("totally_unknown_xyz") is ActionDomain.EXTERNAL_READ
