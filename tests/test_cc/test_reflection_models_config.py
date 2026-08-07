"""reflection_models config lever: live-read depth→model/effort accessor.

Mirrors the ego reconcile_config test harness (repo_root + overlay redirected to
tmp dirs). Verifies defaults, per-depth partial overrides, overlay precedence,
degrade-on-damage, and no-cache live reads.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from genesis.awareness.types import Depth
from genesis.cc import reflection_bridge  # noqa: F401  (ensure package import path)
from genesis.cc.reflection_bridge import reflection_models_config as rmc
from genesis.cc.types import CCModel, EffortLevel


@pytest.fixture
def config_dirs(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """Redirect base + overlay config resolution into tmp dirs.

    Returns ``(base_path, overlay_path)`` — neither file exists initially.
    """
    repo_dir = tmp_path / "repo"
    user_dir = tmp_path / "user_config"
    (repo_dir / "config").mkdir(parents=True)
    user_dir.mkdir(parents=True)
    monkeypatch.setattr(rmc, "repo_root", lambda: repo_dir)
    monkeypatch.setattr("genesis._config_overlay._user_config_dir", lambda: user_dir)
    return (
        repo_dir / "config" / "reflection_models.yaml",
        user_dir / "reflection_models.local.yaml",
    )


# ── defaults (no config on disk) ─────────────────────────────────────────


def test_defaults_when_no_config(config_dirs):
    assert rmc.model_for_depth(Depth.LIGHT) == CCModel.HAIKU
    assert rmc.model_for_depth(Depth.DEEP) == CCModel.SONNET
    assert rmc.model_for_depth(Depth.STRATEGIC) == CCModel.OPUS
    assert rmc.effort_for_depth(Depth.LIGHT) == EffortLevel.LOW
    assert rmc.effort_for_depth(Depth.DEEP) == EffortLevel.XHIGH
    assert rmc.effort_for_depth(Depth.STRATEGIC) == EffortLevel.XHIGH


# ── overrides ────────────────────────────────────────────────────────────


def test_base_override_model(config_dirs):
    base, _ = config_dirs
    base.write_text("deep:\n  model: opus\n")
    assert rmc.model_for_depth(Depth.DEEP) == CCModel.OPUS


def test_partial_override_keeps_other_field(config_dirs):
    """Overriding only deep.effort must keep deep.model from the defaults."""
    base, _ = config_dirs
    base.write_text("deep:\n  effort: max\n")
    assert rmc.effort_for_depth(Depth.DEEP) == EffortLevel.MAX
    assert rmc.model_for_depth(Depth.DEEP) == CCModel.SONNET  # not lost


def test_overlay_wins_over_base(config_dirs):
    base, overlay = config_dirs
    base.write_text("deep:\n  effort: high\n")
    overlay.write_text("deep:\n  effort: max\n")
    assert rmc.effort_for_depth(Depth.DEEP) == EffortLevel.MAX


# ── degrade on damage ────────────────────────────────────────────────────


def test_invalid_model_degrades_to_default(config_dirs, caplog):
    base, _ = config_dirs
    base.write_text("deep:\n  model: gpt-9\n")
    with caplog.at_level("WARNING"):
        assert rmc.model_for_depth(Depth.DEEP) == CCModel.SONNET


def test_invalid_effort_degrades_to_default(config_dirs, caplog):
    base, _ = config_dirs
    base.write_text("deep:\n  effort: turbo\n")
    with caplog.at_level("WARNING"):
        assert rmc.effort_for_depth(Depth.DEEP) == EffortLevel.XHIGH


def test_corrupt_base_degrades_to_defaults(config_dirs):
    base, _ = config_dirs
    base.write_text("{{{{not yaml")
    assert rmc.model_for_depth(Depth.DEEP) == CCModel.SONNET
    assert rmc.effort_for_depth(Depth.DEEP) == EffortLevel.XHIGH


def test_read_live_no_cache(config_dirs):
    base, _ = config_dirs
    base.write_text("deep:\n  effort: max\n")
    assert rmc.effort_for_depth(Depth.DEEP) == EffortLevel.MAX
    base.write_text("deep:\n  effort: high\n")
    assert rmc.effort_for_depth(Depth.DEEP) == EffortLevel.HIGH  # next call, no cache


# ── editor_view: conditional effort exposure (P2 — Codex #1261) ──────────


def test_editor_view_exposes_effort_for_effort_capable_model():
    """Switching a depth to an effort-capable model must surface an effort key so
    the dashboard renders a control (else the hidden hardcoded value is silently
    used). Light shipped as Haiku with no effort; as Sonnet it must gain one."""
    view = rmc.editor_view({"light": {"model": "sonnet"}})
    assert view["light"]["model"] == "sonnet"
    assert view["light"].get("effort")  # a concrete default is exposed
    assert view["light"]["effort"] in {"low", "medium", "high", "xhigh", "max"}


def test_editor_view_hides_effort_for_effortless_model():
    """Haiku ignores --effort at dispatch, so the editor must NOT show an effort
    control for it — even if a stale effort key is present in the served config."""
    view = rmc.editor_view({"light": {"model": "haiku", "effort": "low"}})
    assert view["light"] == {"model": "haiku"}


def test_editor_view_keeps_existing_effort_for_capable_model():
    view = rmc.editor_view({"deep": {"model": "sonnet", "effort": "xhigh"}})
    assert view["deep"] == {"model": "sonnet", "effort": "xhigh"}


def test_editor_view_does_not_mutate_input():
    src = {"light": {"model": "haiku", "effort": "low"}}
    rmc.editor_view(src)
    assert src == {"light": {"model": "haiku", "effort": "low"}}  # unchanged


def test_editor_view_ignores_non_dict_and_unknown():
    view = rmc.editor_view({"light": "oops", "deep": {"model": "opus"}})
    assert view["light"] == "oops"  # left as-is, never crashes
    assert view["deep"].get("effort")  # opus is effort-capable → exposed


# ── shipped base file sanity ─────────────────────────────────────────────


def test_shipped_base_config_values():
    """The shipped config/reflection_models.yaml parses with the intended values.

    Light intentionally omits `effort` (Haiku ignores it); deep/strategic ship
    at xhigh.
    """
    import yaml

    base = Path(__file__).parents[2] / "config" / "reflection_models.yaml"
    cfg = yaml.safe_load(base.read_text())
    assert cfg["light"] == {"model": "haiku"}
    assert cfg["deep"] == {"model": "sonnet", "effort": "xhigh"}
    assert cfg["strategic"] == {"model": "opus", "effort": "xhigh"}
