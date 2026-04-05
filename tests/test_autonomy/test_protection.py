"""Tests for genesis.autonomy.protection — ProtectedPathRegistry."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from genesis.autonomy.protection import ProtectedPathRegistry
from genesis.autonomy.types import ProtectedPathRule, ProtectionLevel

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def rules() -> list[ProtectedPathRule]:
    return [
        ProtectedPathRule("src/genesis/channels/**", ProtectionLevel.CRITICAL,
                          "Relay infrastructure"),
        ProtectedPathRule("config/genesis-bridge.service", ProtectionLevel.CRITICAL,
                          "Bridge systemd unit"),
        ProtectedPathRule("*/secrets.env", ProtectionLevel.CRITICAL, "Secrets"),
        ProtectedPathRule(".claude/settings.json", ProtectionLevel.CRITICAL, "CC hooks"),
        ProtectedPathRule("src/genesis/autonomy/protection.py", ProtectionLevel.CRITICAL,
                          "Self-protection"),
        ProtectedPathRule("config/protected_paths.yaml", ProtectionLevel.CRITICAL,
                          "Protection config"),
        ProtectedPathRule("*.service", ProtectionLevel.CRITICAL, "Systemd units"),
        ProtectedPathRule("/etc/netplan/**", ProtectionLevel.CRITICAL, "Networking"),
        ProtectedPathRule("src/genesis/runtime.py", ProtectionLevel.SENSITIVE,
                          "Runtime bootstrap"),
        ProtectedPathRule("src/genesis/db/schema.py", ProtectionLevel.SENSITIVE,
                          "Database schema"),
        ProtectedPathRule("src/genesis/identity/*.md", ProtectionLevel.SENSITIVE,
                          "Identity files"),
        ProtectedPathRule("config/model_routing.yaml", ProtectionLevel.SENSITIVE,
                          "Model routing"),
    ]


@pytest.fixture()
def registry(rules: list[ProtectedPathRule]) -> ProtectedPathRegistry:
    return ProtectedPathRegistry(rules=rules)


# ---------------------------------------------------------------------------
# Classification tests
# ---------------------------------------------------------------------------

class TestClassify:
    def test_critical_channel_code(self, registry: ProtectedPathRegistry):
        assert registry.classify("src/genesis/channels/bridge.py") is ProtectionLevel.CRITICAL

    def test_critical_channel_nested(self, registry: ProtectedPathRegistry):
        assert registry.classify("src/genesis/channels/telegram/adapter.py") is ProtectionLevel.CRITICAL

    def test_critical_systemd_unit(self, registry: ProtectedPathRegistry):
        assert registry.classify("config/genesis-bridge.service") is ProtectionLevel.CRITICAL

    def test_critical_wildcard_service(self, registry: ProtectedPathRegistry):
        assert registry.classify("config/genesis-watchdog.service") is ProtectionLevel.CRITICAL

    def test_critical_secrets(self, registry: ProtectedPathRegistry):
        assert registry.classify("~/genesis/secrets.env") is ProtectionLevel.CRITICAL

    def test_critical_cc_hooks(self, registry: ProtectedPathRegistry):
        assert registry.classify(".claude/settings.json") is ProtectionLevel.CRITICAL

    def test_critical_self_protection(self, registry: ProtectedPathRegistry):
        """Protection module protects itself."""
        assert registry.classify("src/genesis/autonomy/protection.py") is ProtectionLevel.CRITICAL

    def test_critical_protection_config(self, registry: ProtectedPathRegistry):
        assert registry.classify("config/protected_paths.yaml") is ProtectionLevel.CRITICAL

    def test_critical_networking(self, registry: ProtectedPathRegistry):
        assert registry.classify("/etc/netplan/01-config.yaml") is ProtectionLevel.CRITICAL

    def test_sensitive_runtime(self, registry: ProtectedPathRegistry):
        assert registry.classify("src/genesis/runtime.py") is ProtectionLevel.SENSITIVE

    def test_sensitive_schema(self, registry: ProtectedPathRegistry):
        assert registry.classify("src/genesis/db/schema.py") is ProtectionLevel.SENSITIVE

    def test_sensitive_identity_md(self, registry: ProtectedPathRegistry):
        assert registry.classify("src/genesis/identity/SOUL.md") is ProtectionLevel.SENSITIVE

    def test_sensitive_model_routing(self, registry: ProtectedPathRegistry):
        assert registry.classify("config/model_routing.yaml") is ProtectionLevel.SENSITIVE

    def test_normal_regular_code(self, registry: ProtectedPathRegistry):
        assert registry.classify("src/genesis/awareness/loop.py") is ProtectionLevel.NORMAL

    def test_normal_test_file(self, registry: ProtectedPathRegistry):
        assert registry.classify("tests/test_autonomy/test_types.py") is ProtectionLevel.NORMAL

    def test_normal_config_file(self, registry: ProtectedPathRegistry):
        assert registry.classify("config/outreach.yaml") is ProtectionLevel.NORMAL

    def test_normal_unknown_path(self, registry: ProtectedPathRegistry):
        assert registry.classify("some/random/file.txt") is ProtectionLevel.NORMAL


class TestClassifyWithReason:
    def test_critical_returns_reason(self, registry: ProtectedPathRegistry):
        level, reason = registry.classify_with_reason("src/genesis/channels/bridge.py")
        assert level is ProtectionLevel.CRITICAL
        assert "Relay infrastructure" in reason

    def test_sensitive_returns_reason(self, registry: ProtectedPathRegistry):
        level, reason = registry.classify_with_reason("src/genesis/runtime.py")
        assert level is ProtectionLevel.SENSITIVE
        assert "Runtime bootstrap" in reason

    def test_normal_returns_empty_reason(self, registry: ProtectedPathRegistry):
        level, reason = registry.classify_with_reason("src/genesis/surplus/queue.py")
        assert level is ProtectionLevel.NORMAL
        assert reason == ""


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------

class TestNormalization:
    def test_strips_leading_dot_slash(self, registry: ProtectedPathRegistry):
        assert registry.classify("./src/genesis/channels/bridge.py") is ProtectionLevel.CRITICAL

    def test_handles_absolute_paths(self, registry: ProtectedPathRegistry):
        """Absolute paths like /etc/netplan/ match as-is."""
        assert registry.classify("/etc/netplan/50-cloud-init.yaml") is ProtectionLevel.CRITICAL


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_registry(self):
        reg = ProtectedPathRegistry(rules=[])
        assert reg.classify("anything") is ProtectionLevel.NORMAL

    def test_dotdot_escape_classified_critical(self):
        """Path traversal via ../ must be CRITICAL, not NORMAL."""
        registry = ProtectedPathRegistry(rules=[])
        assert registry.classify("config/../../../etc/passwd") is ProtectionLevel.CRITICAL

    def test_leading_dotdot_classified_critical(self):
        """../../secrets.env must be CRITICAL."""
        registry = ProtectedPathRegistry(rules=[])
        assert registry.classify("../../secrets.env") is ProtectionLevel.CRITICAL

    def test_dotdot_within_repo_still_resolves(self):
        """src/genesis/../genesis/runtime.py normalizes correctly — NOT critical."""
        registry = ProtectedPathRegistry(rules=[])
        result = registry.classify("src/genesis/../genesis/runtime.py")
        assert result is not ProtectionLevel.CRITICAL

    def test_absolute_path_escape_classified_critical(self):
        """Absolute paths like /etc/passwd must be CRITICAL even with no rules."""
        registry = ProtectedPathRegistry(rules=[])
        assert registry.classify("/etc/passwd") is ProtectionLevel.CRITICAL
        assert registry.classify("${HOME}/.ssh/authorized_keys") is ProtectionLevel.CRITICAL

    def test_classify_with_reason_dotdot(self):
        """classify_with_reason also catches traversal."""
        registry = ProtectedPathRegistry(rules=[])
        level, reason = registry.classify_with_reason("../../etc/passwd")
        assert level is ProtectionLevel.CRITICAL
        assert "traversal" in reason.lower()

    def test_classify_with_reason_absolute(self):
        """classify_with_reason catches absolute path escape."""
        registry = ProtectedPathRegistry(rules=[])
        level, reason = registry.classify_with_reason("/etc/shadow")
        assert level is ProtectionLevel.CRITICAL
        assert "traversal" in reason.lower()

    def test_critical_takes_precedence_over_sensitive(self):
        """If a path matches both CRITICAL and SENSITIVE, CRITICAL wins."""
        rules = [
            ProtectedPathRule("src/genesis/**", ProtectionLevel.SENSITIVE, "All genesis code"),
            ProtectedPathRule("src/genesis/channels/**", ProtectionLevel.CRITICAL, "Channels"),
        ]
        reg = ProtectedPathRegistry(rules=rules)
        assert reg.classify("src/genesis/channels/bridge.py") is ProtectionLevel.CRITICAL
        assert reg.classify("src/genesis/awareness/loop.py") is ProtectionLevel.SENSITIVE

    def test_rule_count(self, registry: ProtectedPathRegistry):
        assert registry.rule_count == 12

    def test_get_rules_all(self, registry: ProtectedPathRegistry):
        assert len(registry.get_rules()) == 12

    def test_get_rules_filtered(self, registry: ProtectedPathRegistry):
        critical = registry.get_rules(ProtectionLevel.CRITICAL)
        sensitive = registry.get_rules(ProtectionLevel.SENSITIVE)
        assert len(critical) == 8
        assert len(sensitive) == 4


# ---------------------------------------------------------------------------
# YAML loading
# ---------------------------------------------------------------------------

class TestYamlLoading:
    def test_load_from_real_config(self):
        """Load from the actual config/protected_paths.yaml."""
        config_path = Path(__file__).resolve().parent.parent.parent / "config" / "protected_paths.yaml"
        if not config_path.exists():
            pytest.skip("Config file not at expected path")
        reg = ProtectedPathRegistry.from_yaml(config_path)
        assert reg.rule_count > 0
        # Self-protection: the protection module itself should be CRITICAL
        assert reg.classify("src/genesis/autonomy/protection.py") is ProtectionLevel.CRITICAL

    def test_load_missing_file(self, tmp_path: Path):
        """Missing config → empty registry, all paths NORMAL."""
        reg = ProtectedPathRegistry.from_yaml(tmp_path / "nonexistent.yaml")
        assert reg.rule_count == 0
        assert reg.classify("anything") is ProtectionLevel.NORMAL

    def test_load_invalid_yaml(self, tmp_path: Path):
        """Invalid YAML → empty registry."""
        bad_file = tmp_path / "bad.yaml"
        bad_file.write_text(": : : not valid yaml [[[")
        reg = ProtectedPathRegistry.from_yaml(bad_file)
        assert reg.rule_count == 0

    def test_load_yaml_with_string_entries(self, tmp_path: Path):
        """Config can use plain strings instead of dicts."""
        config = tmp_path / "paths.yaml"
        config.write_text(textwrap.dedent("""\
            critical:
              - "src/danger/**"
            sensitive:
              - "src/careful/**"
        """))
        reg = ProtectedPathRegistry.from_yaml(config)
        assert reg.classify("src/danger/foo.py") is ProtectionLevel.CRITICAL
        assert reg.classify("src/careful/bar.py") is ProtectionLevel.SENSITIVE


# ---------------------------------------------------------------------------
# Prompt formatting
# ---------------------------------------------------------------------------

class TestFormatForPrompt:
    def test_contains_critical_section(self, registry: ProtectedPathRegistry):
        prompt = registry.format_for_prompt()
        assert "CRITICAL" in prompt
        assert "relay" in prompt.lower()

    def test_contains_sensitive_section(self, registry: ProtectedPathRegistry):
        prompt = registry.format_for_prompt()
        assert "SENSITIVE" in prompt

    def test_contains_patterns(self, registry: ProtectedPathRegistry):
        prompt = registry.format_for_prompt()
        assert "src/genesis/channels/**" in prompt

    def test_empty_registry_minimal(self):
        reg = ProtectedPathRegistry(rules=[])
        prompt = reg.format_for_prompt()
        assert "Protected Paths" in prompt
