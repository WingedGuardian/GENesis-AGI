"""Tests for reflex config loading — defaults, malformed input, env kill."""

from __future__ import annotations

from genesis.reflex.config import ReflexConfig, load_reflex_config


class TestDefaults:
    def test_missing_file_defaults_off(self, tmp_path):
        cfg = load_reflex_config(tmp_path / "nope.yaml")
        assert cfg.ingest_enabled is False

    def test_dataclass_default_off(self):
        assert ReflexConfig().ingest_enabled is False


class TestFileLoading:
    def test_enabled_true_loads(self, tmp_path):
        p = tmp_path / "reflex.yaml"
        p.write_text("ingest_enabled: true\n")
        assert load_reflex_config(p).ingest_enabled is True

    def test_malformed_yaml_defaults_off(self, tmp_path):
        p = tmp_path / "reflex.yaml"
        p.write_text("ingest_enabled: [unclosed\n")
        assert load_reflex_config(p).ingest_enabled is False

    def test_non_mapping_defaults_off(self, tmp_path):
        p = tmp_path / "reflex.yaml"
        p.write_text("- just\n- a list\n")
        assert load_reflex_config(p).ingest_enabled is False

    def test_missing_key_defaults_off(self, tmp_path):
        p = tmp_path / "reflex.yaml"
        p.write_text("some_future_key: 1\n")
        assert load_reflex_config(p).ingest_enabled is False


class TestEnvKill:
    def test_env_kill_wins_over_enabled_config(self, tmp_path, monkeypatch):
        p = tmp_path / "reflex.yaml"
        p.write_text("ingest_enabled: true\n")
        monkeypatch.setenv("GENESIS_REFLEX_INGEST_OFF", "1")
        cfg = load_reflex_config(p)
        assert cfg.ingest_enabled is False
        assert cfg.source == "env_kill"

    def test_env_kill_accepts_true_spelling(self, tmp_path, monkeypatch):
        p = tmp_path / "reflex.yaml"
        p.write_text("ingest_enabled: true\n")
        monkeypatch.setenv("GENESIS_REFLEX_INGEST_OFF", "true")
        assert load_reflex_config(p).ingest_enabled is False

    def test_env_zero_does_not_kill(self, tmp_path, monkeypatch):
        p = tmp_path / "reflex.yaml"
        p.write_text("ingest_enabled: true\n")
        monkeypatch.setenv("GENESIS_REFLEX_INGEST_OFF", "0")
        assert load_reflex_config(p).ingest_enabled is True

    def test_shipped_repo_config_is_off(self):
        # the committed config/reflex.yaml must ship dark on every install —
        # read the shipped file directly (a local overlay or env kill on the
        # running install must not affect this assertion)
        import yaml

        from genesis.reflex.config import _CONFIG_PATH

        raw = yaml.safe_load(_CONFIG_PATH.read_text())
        assert raw["ingest_enabled"] is False
