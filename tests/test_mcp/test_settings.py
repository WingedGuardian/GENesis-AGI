"""Tests for settings MCP tools — list, get, update config domains."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from genesis.mcp.health.settings import (
    _DOMAIN_REGISTRY,
    _atomic_yaml_write,
    _deep_merge,
    _impl_settings_get,
    _impl_settings_list,
    _impl_settings_update,
    _validate_inbox_monitor,
    _validate_resilience,
    _validate_tts,
)
from genesis.mcp.health_mcp import mcp

# ── Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture()
def config_dir(tmp_path: Path) -> Path:
    """Provide a temporary config directory."""
    return tmp_path


@pytest.fixture(autouse=True)
def _patch_config_dir(config_dir: Path):
    """Redirect all config reads/writes to the temp dir."""
    with patch("genesis.mcp.health.settings._CONFIG_DIR", config_dir):
        yield


def _write_config(config_dir: Path, filename: str, data: dict) -> Path:
    """Write a YAML config file into the temp config dir."""
    path = config_dir / filename
    with open(path, "w") as f:
        yaml.dump(data, f)
    return path


# ── settings_list ──────────────────────────────────────────────────────


async def test_settings_list_returns_all_domains():
    result = await _impl_settings_list()
    names = {d["domain"] for d in result}
    assert names == set(_DOMAIN_REGISTRY.keys())


async def test_settings_list_includes_metadata():
    result = await _impl_settings_list()
    for item in result:
        assert "domain" in item
        assert "description" in item
        assert "readonly" in item
        assert "needs_restart" in item
        assert "dedicated_tool" in item


async def test_settings_list_marks_outreach_as_dedicated():
    result = await _impl_settings_list()
    outreach = next(d for d in result if d["domain"] == "outreach")
    assert outreach["dedicated_tool"] == "outreach_preferences"


# ── settings_get ───────────────────────────────────────────────────────


async def test_settings_get_tts(config_dir: Path):
    _write_config(config_dir, "tts.yaml", {
        "provider": "elevenlabs",
        "elevenlabs": {"stability": 0.9, "speed": 1.1},
    })
    result = await _impl_settings_get("tts")
    assert result["domain"] == "tts"
    assert result["config"]["provider"] == "elevenlabs"
    assert result["config"]["elevenlabs"]["stability"] == 0.9
    assert result["readonly"] is False
    assert result["needs_restart"] is False
    assert result["source_file"] == "config/tts.yaml"


async def test_settings_get_readonly_domain(config_dir: Path):
    _write_config(config_dir, "autonomy.yaml", {"defaults": {"direct_session": 1}})
    result = await _impl_settings_get("autonomy")
    assert result["readonly"] is True
    assert result["config"]["defaults"]["direct_session"] == 1


async def test_settings_get_dedicated_tool_redirect():
    result = await _impl_settings_get("outreach")
    assert "note" in result
    assert "outreach_preferences" in result["note"]
    assert result["dedicated_tool"] == "outreach_preferences"
    assert "config" not in result  # No config returned for redirects


async def test_settings_get_unknown_domain():
    result = await _impl_settings_get("nonexistent")
    assert "error" in result
    assert "nonexistent" in result["error"]


async def test_settings_get_missing_file():
    result = await _impl_settings_get("tts")
    assert result["config"] == {}  # Empty dict for missing file


# ── settings_update ────────────────────────────────────────────────────


async def test_settings_update_tts(config_dir: Path):
    _write_config(config_dir, "tts.yaml", {
        "provider": "elevenlabs",
        "elevenlabs": {"stability": 0.85, "speed": 1.1},
    })
    result = await _impl_settings_update("tts", {
        "elevenlabs": {"stability": 0.9},
    })
    assert result["status"] == "applied"
    assert result["needs_restart"] is False
    assert "local_override_file" in result

    # Changes go to .local.yaml; base file is unchanged
    local = yaml.safe_load((config_dir / "tts.local.yaml").read_text())
    assert local["elevenlabs"]["stability"] == 0.9
    # Base file should be unchanged
    base = yaml.safe_load((config_dir / "tts.yaml").read_text())
    assert base["elevenlabs"]["stability"] == 0.85  # Unchanged in base

    # Merged view (via settings_get) should show updated value
    from genesis.mcp.health.settings import _load_yaml_merged
    merged = _load_yaml_merged("tts.yaml")
    assert merged["elevenlabs"]["stability"] == 0.9
    assert merged["elevenlabs"]["speed"] == 1.1  # Preserved from base
    assert merged["provider"] == "elevenlabs"  # Preserved from base


async def test_settings_update_tts_validation_error(config_dir: Path):
    _write_config(config_dir, "tts.yaml", {"provider": "elevenlabs"})
    result = await _impl_settings_update("tts", {
        "elevenlabs": {"stability": 5.0},
    })
    assert result["error"] == "validation failed"
    assert any("stability" in e for e in result["validation_errors"])


async def test_settings_update_resilience(config_dir: Path):
    _write_config(config_dir, "resilience.yaml", {
        "flapping": {"transition_count": 3, "window_seconds": 900},
        "cc": {"max_sessions_per_hour": 20},
    })
    result = await _impl_settings_update("resilience", {
        "cc": {"max_sessions_per_hour": 30},
    })
    assert result["status"] == "applied"
    assert result["needs_restart"] is True
    assert "note" in result  # Restart note

    # Changes in .local.yaml; merged view has both
    from genesis.mcp.health.settings import _load_yaml_merged
    merged = _load_yaml_merged("resilience.yaml")
    assert merged["cc"]["max_sessions_per_hour"] == 30
    assert merged["flapping"]["transition_count"] == 3  # Preserved from base


async def test_settings_update_inbox_monitor(config_dir: Path):
    _write_config(config_dir, "inbox_monitor.yaml", {
        "inbox_monitor": {
            "enabled": True,
            "batch_size": 1,
            "model": "sonnet",
            "timezone": "America/New_York",
        },
    })
    result = await _impl_settings_update("inbox_monitor", {
        "inbox_monitor": {"batch_size": 3, "model": "opus"},
    })
    assert result["status"] == "applied"
    assert result["needs_restart"] is True

    from genesis.mcp.health.settings import _load_yaml_merged
    merged = _load_yaml_merged("inbox_monitor.yaml")
    assert merged["inbox_monitor"]["batch_size"] == 3
    assert merged["inbox_monitor"]["model"] == "opus"
    assert merged["inbox_monitor"]["timezone"] == "America/New_York"  # Preserved from base


async def test_settings_update_inbox_timezone_ignored(config_dir: Path):
    """Timezone field is silently ignored — uses system timezone now."""
    _write_config(config_dir, "inbox_monitor.yaml", {
        "inbox_monitor": {"enabled": True},
    })
    result = await _impl_settings_update("inbox_monitor", {
        "inbox_monitor": {"timezone": "Mars/Olympus_Mons"},
    })
    # No validation error — timezone key is ignored, passes through as unknown YAML
    assert "error" not in result


async def test_settings_update_readonly_rejected():
    result = await _impl_settings_update("autonomy", {"defaults": {"direct_session": 5}})
    assert "error" in result
    assert "read-only" in result["error"]


async def test_settings_update_dedicated_tool_rejected():
    result = await _impl_settings_update("outreach", {"quiet_hours": {"start": "23:00"}})
    assert "error" in result
    assert "outreach_preferences" in result["error"]


async def test_settings_update_unknown_domain():
    result = await _impl_settings_update("nonexistent", {"key": "val"})
    assert "error" in result
    assert "nonexistent" in result["error"]


async def test_settings_update_dry_run(config_dir: Path):
    _write_config(config_dir, "tts.yaml", {"provider": "elevenlabs"})
    result = await _impl_settings_update("tts", {
        "elevenlabs": {"stability": 0.9},
    }, dry_run=True)
    assert result["status"] == "dry_run_ok"
    assert result["changes_applied"] == {"elevenlabs": {"stability": 0.9}}

    # Verify file was NOT modified
    written = yaml.safe_load((config_dir / "tts.yaml").read_text())
    assert "elevenlabs" not in written  # Original had no elevenlabs key


async def test_settings_update_creates_file(config_dir: Path):
    """Update should create the local overlay file (base may not exist)."""
    assert not (config_dir / "tts.yaml").exists()
    result = await _impl_settings_update("tts", {"provider": "fish_audio"})
    assert result["status"] == "applied"
    # Changes go to .local.yaml
    assert (config_dir / "tts.local.yaml").exists()
    written = yaml.safe_load((config_dir / "tts.local.yaml").read_text())
    assert written["provider"] == "fish_audio"


async def test_settings_update_write_failure(config_dir: Path):
    """Write failure returns error without crashing."""
    _write_config(config_dir, "tts.yaml", {"provider": "elevenlabs"})
    with patch(
        "genesis.mcp.health.settings._atomic_yaml_write",
        side_effect=OSError("disk full"),
    ):
        result = await _impl_settings_update("tts", {"provider": "fish_audio"})
    assert "error" in result
    assert "write" in result["error"].lower()


async def test_settings_update_inbox_flat_changes_auto_wrapped(config_dir: Path):
    """Flat changes (without inbox_monitor wrapper) are auto-wrapped."""
    _write_config(config_dir, "inbox_monitor.yaml", {
        "inbox_monitor": {"enabled": True, "batch_size": 1},
    })
    result = await _impl_settings_update("inbox_monitor", {"batch_size": 5})
    assert result["status"] == "applied"

    from genesis.mcp.health.settings import _load_yaml_merged
    merged = _load_yaml_merged("inbox_monitor.yaml")
    assert merged["inbox_monitor"]["batch_size"] == 5
    assert merged["inbox_monitor"]["enabled"] is True  # Preserved from base


# ── deep_merge ─────────────────────────────────────────────────────────


def test_deep_merge_basic():
    base = {"a": 1, "b": 2}
    overlay = {"b": 3, "c": 4}
    assert _deep_merge(base, overlay) == {"a": 1, "b": 3, "c": 4}


def test_deep_merge_nested():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    overlay = {"a": {"y": 99, "z": 100}}
    result = _deep_merge(base, overlay)
    assert result == {"a": {"x": 1, "y": 99, "z": 100}, "b": 3}


def test_deep_merge_list_replaces():
    base = {"items": [1, 2, 3]}
    overlay = {"items": [4, 5]}
    assert _deep_merge(base, overlay) == {"items": [4, 5]}


def test_deep_merge_type_override():
    base = {"a": {"nested": True}}
    overlay = {"a": "flat_string"}
    assert _deep_merge(base, overlay) == {"a": "flat_string"}


def test_deep_merge_does_not_mutate_base():
    base = {"a": {"x": 1}}
    overlay = {"a": {"y": 2}}
    _deep_merge(base, overlay)
    assert base == {"a": {"x": 1}}


# ── validators ─────────────────────────────────────────────────────────


class TestValidateTTS:
    def test_valid_changes(self):
        assert _validate_tts({"provider": "fish_audio"}) == []

    def test_invalid_provider(self):
        errs = _validate_tts({"provider": "openai"})
        assert len(errs) == 1
        assert "provider" in errs[0]

    def test_stability_out_of_range(self):
        errs = _validate_tts({"elevenlabs": {"stability": 1.5}})
        assert len(errs) == 1
        assert "stability" in errs[0]

    def test_speed_out_of_range(self):
        errs = _validate_tts({"elevenlabs": {"speed": 2.0}})
        assert len(errs) == 1
        assert "speed" in errs[0]

    def test_unknown_key(self):
        errs = _validate_tts({"unknown_key": 42})
        assert len(errs) == 1
        assert "Unknown key" in errs[0]

    def test_multiple_errors(self):
        errs = _validate_tts({
            "provider": "bad",
            "elevenlabs": {"stability": 5.0, "speed": 0.1},
        })
        assert len(errs) == 3

    def test_valid_full_elevenlabs(self):
        errs = _validate_tts({
            "elevenlabs": {
                "stability": 0.85,
                "similarity_boost": 0.7,
                "style": 0.3,
                "speed": 1.1,
            },
        })
        assert errs == []


class TestValidateResilience:
    def test_valid_changes(self):
        errs = _validate_resilience({"cc": {"max_sessions_per_hour": 30}})
        assert errs == []

    def test_negative_transition_count(self):
        errs = _validate_resilience({"flapping": {"transition_count": -1}})
        assert len(errs) == 1

    def test_throttle_out_of_range(self):
        errs = _validate_resilience({"cc": {"throttle_threshold_pct": 1.5}})
        assert len(errs) == 1
        assert "throttle_threshold_pct" in errs[0]

    def test_unknown_key(self):
        errs = _validate_resilience({"bogus": True})
        assert len(errs) == 1


class TestValidateInboxMonitor:
    def test_valid_changes(self):
        errs = _validate_inbox_monitor({"inbox_monitor": {"batch_size": 3}})
        assert errs == []

    def test_batch_size_too_large(self):
        errs = _validate_inbox_monitor({"inbox_monitor": {"batch_size": 99}})
        assert len(errs) == 1

    def test_invalid_model(self):
        errs = _validate_inbox_monitor({"inbox_monitor": {"model": "gpt4"}})
        assert len(errs) == 1

    def test_invalid_effort(self):
        errs = _validate_inbox_monitor({"inbox_monitor": {"effort": "insane"}})
        assert len(errs) == 1

    def test_timezone_ignored(self):
        """Timezone field is no longer validated — uses system timezone."""
        errs = _validate_inbox_monitor({"inbox_monitor": {"timezone": "Fake/Zone"}})
        assert errs == []

    def test_flat_changes_also_work(self):
        """Changes without the inbox_monitor wrapper should also validate."""
        errs = _validate_inbox_monitor({"batch_size": 2})
        assert errs == []


# ── atomic write ───────────────────────────────────────────────────────


def test_atomic_write(config_dir: Path):
    data = {"key": "value", "nested": {"a": 1}}
    with patch("genesis.mcp.health.settings._CONFIG_DIR", config_dir):
        path = _atomic_yaml_write("test.yaml", data)
    assert path.exists()
    loaded = yaml.safe_load(path.read_text())
    assert loaded == data


def test_atomic_write_overwrites(config_dir: Path):
    (config_dir / "test.yaml").write_text("old: data\n")
    with patch("genesis.mcp.health.settings._CONFIG_DIR", config_dir):
        _atomic_yaml_write("test.yaml", {"new": "data"})
    loaded = yaml.safe_load((config_dir / "test.yaml").read_text())
    assert loaded == {"new": "data"}


# ── tool registration ─────────────────────────────────────────────────


async def test_settings_tools_registered():
    tools = await mcp.get_tools()
    for name in ["settings_list", "settings_get", "settings_update"]:
        assert name in tools, f"Missing tool: {name}"
