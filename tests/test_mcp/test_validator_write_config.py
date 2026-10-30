"""Only a valid local false boolean can clear the calibration write lock."""

import importlib
import importlib.util

import pytest


@pytest.mark.parametrize("base,overlay,expected", [
    (None, "dry_run_only: false", True),
    ("dry_run_only: false", "dry_run_only: false", True),
    ("[]", "dry_run_only: false", True),
    ("dry_run_only: true", None, True),
    ("dry_run_only: true", "dry_run_only: false", False),
    ("dry_run_only: true", "dry_run_only: true", True),
    ("dry_run_only: true", "dry_run_only: 'false'", True),
    ("dry_run_only: true", "dry_run_only: 0", True),
    ("dry_run_only: true", "dry_run_only: null", True),
    ("dry_run_only: true", "[", True),
    ("dry_run_only: true", "[]", True),
    ("dry_run_only: true", "other: false", True),
    ("dry_run_only: true", "dry_run_only: false\nother: false", True),
])
def test_actual_config_files_fail_toward_preview(tmp_path, monkeypatch, base, overlay, expected):
    name = "genesis.session_awareness.codex_validator_config"
    assert importlib.util.find_spec(name) is not None, "server-side calibration gate is required"
    config = importlib.import_module(name)
    from genesis import _config_overlay

    path = tmp_path / "codex_validator.yaml"
    user = tmp_path / "user"
    user.mkdir()
    monkeypatch.setattr(config, "_base_path", lambda: path)
    monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: user)
    if base is not None:
        path.write_text(base)
    if overlay is not None:
        path.with_suffix(".local.yaml").write_text(overlay)
    assert config.dry_run_only() is expected
    # No cached decision: every actual call reloads user-first overrides.
    (user / "codex_validator.local.yaml").write_text("dry_run_only: true")
    assert config.dry_run_only() is True
