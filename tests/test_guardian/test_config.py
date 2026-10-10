"""Tests for Guardian configuration loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from genesis.guardian.config import (
    SWAP_CEILING_OFF,
    GuardianConfig,
    load_config,
    load_secrets,
)


class TestGuardianConfigDefaults:
    """Test that defaults are sensible."""

    def test_default_container_name(self) -> None:
        cfg = GuardianConfig()
        assert cfg.container_name == "genesis"

    def test_default_container_ip_empty(self) -> None:
        cfg = GuardianConfig()
        assert cfg.container_ip == ""  # Auto-detected at runtime

    def test_default_health_port(self) -> None:
        cfg = GuardianConfig()
        assert cfg.health_api_port == 5000

    def test_health_url_with_explicit_ip(self) -> None:
        cfg = GuardianConfig(container_ip="10.0.0.1")
        assert cfg.health_url == "http://10.0.0.1:5000"

    def test_state_path_expands_user(self) -> None:
        cfg = GuardianConfig()
        assert "~" not in str(cfg.state_path)

    def test_probe_defaults(self) -> None:
        cfg = GuardianConfig()
        assert cfg.probes.probe_timeout_s == 10
        assert cfg.probes.ping_count == 1

    def test_confirmation_defaults(self) -> None:
        cfg = GuardianConfig()
        assert cfg.confirmation.recheck_delay_s == 30
        assert cfg.confirmation.max_recheck_attempts == 3
        assert cfg.confirmation.required_failed_signals == 2
        assert cfg.confirmation.bootstrap_grace_s == 300

    def test_cc_defaults(self) -> None:
        cfg = GuardianConfig()
        assert cfg.cc.enabled is True
        assert cfg.cc.model == "opus"
        assert cfg.cc.timeout_s == 3600
        assert cfg.cc.max_turns == 50

    def test_briefing_defaults(self) -> None:
        cfg = GuardianConfig()
        assert cfg.briefing.enabled is True
        assert cfg.briefing.shared_subdir == "shared"
        assert cfg.briefing.briefing_filename == "guardian_briefing.md"
        assert cfg.briefing.max_age_s == 600

    def test_briefing_path_property(self) -> None:
        cfg = GuardianConfig(state_dir="/tmp/test-guardian")
        assert cfg.briefing_path == Path("/tmp/test-guardian/shared/briefing/guardian_briefing.md")

    def test_snapshot_defaults(self) -> None:
        cfg = GuardianConfig()
        # 2 = the healthy lifeline + the newest pre-recovery coexist.
        assert cfg.snapshots.retention == 2
        assert cfg.snapshots.prefix == "guardian-"
        assert cfg.snapshots.take_pre_recovery is True
        assert cfg.snapshots.healthy_enabled is True
        assert cfg.snapshots.max_pool_usage_pct == 80.0


class TestLoadConfig:
    """Test YAML config loading."""

    def test_load_from_yaml(self, config_yaml: Path) -> None:
        cfg = load_config(config_yaml)
        assert cfg.container_name == "test-genesis"
        assert cfg.container_ip == "10.0.0.1"
        assert cfg.health_api_port == 5555
        assert cfg.check_interval_s == 15

    def test_load_sub_configs(self, config_yaml: Path) -> None:
        cfg = load_config(config_yaml)
        assert cfg.probes.probe_timeout_s == 5
        assert cfg.probes.ping_count == 2
        assert cfg.confirmation.recheck_delay_s == 10
        assert cfg.confirmation.max_recheck_attempts == 2

    def test_load_cc_config(self, config_yaml: Path) -> None:
        cfg = load_config(config_yaml)
        assert cfg.cc.enabled is False
        assert cfg.cc.model == "haiku"

    def test_missing_file_returns_defaults(self, tmp_path: Path) -> None:
        cfg = load_config(tmp_path / "nonexistent.yaml")
        assert cfg.container_name == "genesis"
        assert cfg.health_api_port == 5000

    def test_empty_yaml_returns_defaults(self, tmp_path: Path) -> None:
        p = tmp_path / "empty.yaml"
        p.write_text("")
        cfg = load_config(p)
        assert cfg.container_name == "genesis"

    def test_unknown_yaml_keys_ignored(self, tmp_path: Path) -> None:
        p = tmp_path / "extra.yaml"
        p.write_text("probes:\n  probe_timeout_s: 5\n  unknown_key: 42\n")
        cfg = load_config(p)
        assert cfg.probes.probe_timeout_s == 5


class TestEnvOverrides:
    """Test environment variable overrides."""

    def test_container_name_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GUARDIAN_CONTAINER_NAME", "my-container")
        cfg = load_config(Path("/nonexistent"))
        assert cfg.container_name == "my-container"

    def test_container_ip_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GUARDIAN_CONTAINER_IP", "192.168.1.100")
        cfg = load_config(Path("/nonexistent"))
        assert cfg.container_ip == "192.168.1.100"

    def test_health_port_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GUARDIAN_HEALTH_PORT", "8080")
        cfg = load_config(Path("/nonexistent"))
        assert cfg.health_api_port == 8080

    def test_telegram_token_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GUARDIAN_TELEGRAM_BOT_TOKEN", "test-token")
        cfg = load_config(Path("/nonexistent"))
        assert cfg.alert.telegram_bot_token == "test-token"

    def test_cc_enabled_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GUARDIAN_CC_ENABLED", "false")
        cfg = load_config(Path("/nonexistent"))
        assert cfg.cc.enabled is False

    def test_env_overrides_yaml(
        self, config_yaml: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GUARDIAN_CONTAINER_NAME", "override-name")
        cfg = load_config(config_yaml)
        # Env overrides YAML
        assert cfg.container_name == "override-name"
        # YAML values still loaded for non-overridden fields
        assert cfg.container_ip == "10.0.0.1"

    def test_guardian_config_env_var(
        self, config_yaml: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("GUARDIAN_CONFIG", str(config_yaml))
        cfg = load_config()
        assert cfg.container_name == "test-genesis"


class TestLoadSecrets:
    """Test secrets file loading."""

    def test_load_secrets_file(self, tmp_path: Path) -> None:
        p = tmp_path / "secrets.env"
        p.write_text(
            "TELEGRAM_BOT_TOKEN=abc123\n"
            "TELEGRAM_CHAT_ID=456\n"
            "# This is a comment\n"
            "\n"
            "SOME_KEY='quoted value'\n"
        )
        secrets = load_secrets(p)
        assert secrets["TELEGRAM_BOT_TOKEN"] == "abc123"
        assert secrets["TELEGRAM_CHAT_ID"] == "456"
        assert secrets["SOME_KEY"] == "quoted value"

    def test_missing_secrets_returns_empty(self, tmp_path: Path) -> None:
        secrets = load_secrets(tmp_path / "nonexistent.env")
        assert secrets == {}

    def test_double_quoted_values(self, tmp_path: Path) -> None:
        p = tmp_path / "secrets.env"
        p.write_text('KEY="double quoted"\n')
        secrets = load_secrets(p)
        assert secrets["KEY"] == "double quoted"


class TestSwapCeilingPctValidation:
    """swap_ceiling_pct: a percentage of HOST SwapTotal (0, 100], "off", or None."""

    def test_default_is_none(self) -> None:
        cfg = GuardianConfig()
        assert cfg.swap_ceiling_pct is None

    def test_valid_value_loads_as_float(self, tmp_path: Path) -> None:
        p = tmp_path / "g.yaml"
        p.write_text("swap_ceiling_pct: 50\n")
        cfg = load_config(p)
        assert cfg.swap_ceiling_pct == 50.0

    def test_null_stays_none(self, tmp_path: Path) -> None:
        p = tmp_path / "g.yaml"
        p.write_text("swap_ceiling_pct: null\n")
        cfg = load_config(p)
        assert cfg.swap_ceiling_pct is None

    def test_zero_is_rejected_not_silently_disabling(self, tmp_path: Path) -> None:
        """0 would mean 'cap swap at nothing' — the opposite of this knob's
        purpose — so it must fall back to None (uncapped), not pass through."""
        p = tmp_path / "g.yaml"
        p.write_text("swap_ceiling_pct: 0\n")
        cfg = load_config(p)
        assert cfg.swap_ceiling_pct is None

    def test_over_100_is_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "g.yaml"
        p.write_text("swap_ceiling_pct: 150\n")
        cfg = load_config(p)
        assert cfg.swap_ceiling_pct is None

    def test_negative_is_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "g.yaml"
        p.write_text("swap_ceiling_pct: -5\n")
        cfg = load_config(p)
        assert cfg.swap_ceiling_pct is None

    def test_string_value_is_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "g.yaml"
        p.write_text("swap_ceiling_pct: fifty\n")
        cfg = load_config(p)
        assert cfg.swap_ceiling_pct is None

    def test_bool_true_is_rejected_not_treated_as_one_percent(self, tmp_path: Path) -> None:
        """NOTE N3: bool is an int subclass in Python, so the bool check must
        run BEFORE the numeric-range check, or `true` silently becomes 1%."""
        p = tmp_path / "g.yaml"
        p.write_text("swap_ceiling_pct: true\n")
        cfg = load_config(p)
        assert cfg.swap_ceiling_pct is None

    @pytest.mark.parametrize("raw", ["off", "OFF", "false", "no", '"off"', '" Off "'])
    def test_off_spellings_load_as_off(self, tmp_path: Path, raw: str) -> None:
        """YAML 1.1 reads a bare off/false/no as boolean False, so all of them
        mean the same request as the quoted string: remove the ceiling."""
        p = tmp_path / "g.yaml"
        p.write_text(f"swap_ceiling_pct: {raw}\n")
        cfg = load_config(p)
        assert cfg.swap_ceiling_pct == SWAP_CEILING_OFF

    def test_huge_int_is_rejected_without_crashing_config_load(self, tmp_path: Path) -> None:
        """Codex P2: math.isfinite(float(huge_int)) raises OverflowError
        rather than returning False, which would abort config loading
        entirely instead of the intended warn-and-ignore. A ~310-digit int is
        caught by the range check before any float conversion is attempted."""
        p = tmp_path / "g.yaml"
        p.write_text("swap_ceiling_pct: " + ("9" * 310) + "\n")
        cfg = load_config(p)  # must not raise
        assert cfg.swap_ceiling_pct is None

    def test_nan_is_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "g.yaml"
        p.write_text("swap_ceiling_pct: .nan\n")
        cfg = load_config(p)
        assert cfg.swap_ceiling_pct is None

    def test_infinity_is_rejected(self, tmp_path: Path) -> None:
        p = tmp_path / "g.yaml"
        p.write_text("swap_ceiling_pct: .inf\n")
        cfg = load_config(p)
        assert cfg.swap_ceiling_pct is None

    def test_float_in_range_loads(self, tmp_path: Path) -> None:
        p = tmp_path / "g.yaml"
        p.write_text("swap_ceiling_pct: 33.5\n")
        cfg = load_config(p)
        assert cfg.swap_ceiling_pct == 33.5

    def test_boundary_100_is_accepted(self, tmp_path: Path) -> None:
        p = tmp_path / "g.yaml"
        p.write_text("swap_ceiling_pct: 100\n")
        cfg = load_config(p)
        assert cfg.swap_ceiling_pct == 100.0
