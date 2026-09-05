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
    _validate_cc_foreground_reaper,
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
    with (
        patch("genesis.mcp.health.settings._CONFIG_DIR", config_dir),
        patch("genesis.mcp.health.settings._USER_CONFIG_DIR", config_dir),
    ):
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


async def test_settings_list_outreach_is_writable():
    result = await _impl_settings_list()
    outreach = next(d for d in result if d["domain"] == "outreach")
    assert outreach["dedicated_tool"] is None
    assert outreach["readonly"] is False


# ── settings_get ───────────────────────────────────────────────────────


async def test_settings_get_tts(config_dir: Path):
    _write_config(
        config_dir,
        "tts.yaml",
        {
            "provider": "elevenlabs",
            "elevenlabs": {"stability": 0.9, "speed": 1.1},
        },
    )
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


async def test_settings_get_outreach_returns_config():
    result = await _impl_settings_get("outreach")
    assert "config" in result
    assert result["readonly"] is False
    assert result["domain"] == "outreach"


async def test_settings_get_unknown_domain():
    result = await _impl_settings_get("nonexistent")
    assert "error" in result
    assert "nonexistent" in result["error"]


async def test_settings_get_missing_file():
    result = await _impl_settings_get("tts")
    assert result["config"] == {}  # Empty dict for missing file


# ── settings_update ────────────────────────────────────────────────────


async def test_settings_update_tts(config_dir: Path):
    _write_config(
        config_dir,
        "tts.yaml",
        {
            "provider": "elevenlabs",
            "elevenlabs": {"stability": 0.85, "speed": 1.1},
        },
    )
    result = await _impl_settings_update(
        "tts",
        {
            "elevenlabs": {"stability": 0.9},
        },
    )
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
    result = await _impl_settings_update(
        "tts",
        {
            "elevenlabs": {"stability": 5.0},
        },
    )
    assert result["error"] == "validation failed"
    assert any("stability" in e for e in result["validation_errors"])


async def test_settings_update_resilience(config_dir: Path):
    _write_config(
        config_dir,
        "resilience.yaml",
        {
            "flapping": {"transition_count": 3, "window_seconds": 900},
            "cc": {"max_sessions_per_hour": 20},
        },
    )
    result = await _impl_settings_update(
        "resilience",
        {
            "cc": {"max_sessions_per_hour": 30},
        },
    )
    assert result["status"] == "applied"
    assert result["needs_restart"] is True
    assert "note" in result  # Restart note

    # Changes in .local.yaml; merged view has both
    from genesis.mcp.health.settings import _load_yaml_merged

    merged = _load_yaml_merged("resilience.yaml")
    assert merged["cc"]["max_sessions_per_hour"] == 30
    assert merged["flapping"]["transition_count"] == 3  # Preserved from base


async def test_settings_update_inbox_monitor(config_dir: Path):
    _write_config(
        config_dir,
        "inbox_monitor.yaml",
        {
            "inbox_monitor": {
                "enabled": True,
                "batch_size": 1,
                "model": "sonnet",
                "timezone": "Europe/Berlin",
            },
        },
    )
    result = await _impl_settings_update(
        "inbox_monitor",
        {
            "inbox_monitor": {"batch_size": 3, "model": "opus"},
        },
    )
    assert result["status"] == "applied"
    assert result["needs_restart"] is True

    from genesis.mcp.health.settings import _load_yaml_merged

    merged = _load_yaml_merged("inbox_monitor.yaml")
    assert merged["inbox_monitor"]["batch_size"] == 3
    assert merged["inbox_monitor"]["model"] == "opus"
    assert merged["inbox_monitor"]["timezone"] == "Europe/Berlin"  # Preserved from base


async def test_settings_update_inbox_timezone_ignored(config_dir: Path):
    """Timezone field is silently ignored — uses system timezone now."""
    _write_config(
        config_dir,
        "inbox_monitor.yaml",
        {
            "inbox_monitor": {"enabled": True},
        },
    )
    result = await _impl_settings_update(
        "inbox_monitor",
        {
            "inbox_monitor": {"timezone": "Mars/Olympus_Mons"},
        },
    )
    # No validation error — timezone key is ignored, passes through as unknown YAML
    assert "error" not in result


async def test_settings_update_readonly_rejected():
    result = await _impl_settings_update("autonomy", {"defaults": {"direct_session": 5}})
    assert "error" in result
    assert "read-only" in result["error"]


async def test_settings_update_outreach_applies(config_dir: Path):
    _write_config(config_dir, "outreach.yaml", {"quiet_hours": {"start": "22:00", "end": "08:00"}})
    result = await _impl_settings_update("outreach", {"quiet_hours": {"start": "23:00"}})
    assert result.get("status") == "applied"
    assert result["changes_applied"]["quiet_hours"]["start"] == "23:00"


async def test_settings_update_unknown_domain():
    result = await _impl_settings_update("nonexistent", {"key": "val"})
    assert "error" in result
    assert "nonexistent" in result["error"]


async def test_settings_update_dry_run(config_dir: Path):
    _write_config(config_dir, "tts.yaml", {"provider": "elevenlabs"})
    result = await _impl_settings_update(
        "tts",
        {
            "elevenlabs": {"stability": 0.9},
        },
        dry_run=True,
    )
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
    _write_config(
        config_dir,
        "inbox_monitor.yaml",
        {
            "inbox_monitor": {"enabled": True, "batch_size": 1},
        },
    )
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
        errs = _validate_tts(
            {
                "provider": "bad",
                "elevenlabs": {"stability": 5.0, "speed": 0.1},
            }
        )
        assert len(errs) == 3

    def test_valid_full_elevenlabs(self):
        errs = _validate_tts(
            {
                "elevenlabs": {
                    "stability": 0.85,
                    "similarity_boost": 0.7,
                    "style": 0.3,
                    "speed": 1.1,
                },
            }
        )
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
    with patch("genesis.mcp.health.settings._USER_CONFIG_DIR", config_dir):
        path = _atomic_yaml_write("test.yaml", data)
    assert path.exists()
    loaded = yaml.safe_load(path.read_text())
    assert loaded == data


def test_atomic_write_overwrites(config_dir: Path):
    (config_dir / "test.yaml").write_text("old: data\n")
    with patch("genesis.mcp.health.settings._USER_CONFIG_DIR", config_dir):
        _atomic_yaml_write("test.yaml", {"new": "data"})
    loaded = yaml.safe_load((config_dir / "test.yaml").read_text())
    assert loaded == {"new": "data"}


# ── tool registration ─────────────────────────────────────────────────


async def test_settings_tools_registered():
    tools = await mcp.get_tools()
    for name in ["settings_list", "settings_get", "settings_update"]:
        assert name in tools, f"Missing tool: {name}"


class TestValidateCcForegroundReaper:
    def test_valid_changes(self):
        assert _validate_cc_foreground_reaper({"mode": "notify", "idle_hours": 12}) == []
        assert _validate_cc_foreground_reaper(
            {"close_dead": False, "dead_process_minutes": 45}
        ) == []
        assert _validate_cc_foreground_reaper({"close_dead": "yes"}) != []

    def test_invalid_mode(self):
        errs = _validate_cc_foreground_reaper({"mode": "notfy"})
        assert len(errs) == 1 and "mode" in errs[0]

    def test_non_positive_int(self):
        errs = _validate_cc_foreground_reaper({"idle_hours": 0})
        assert len(errs) == 1 and "idle_hours" in errs[0]

    def test_unknown_key(self):
        errs = _validate_cc_foreground_reaper({"nope": 1})
        assert len(errs) == 1 and "nope" in errs[0]

    def test_registered(self):
        from genesis.mcp.health.settings import _DOMAIN_REGISTRY, _DOMAIN_VALIDATORS

        assert "cc_foreground_reaper" in _DOMAIN_REGISTRY
        assert "cc_foreground_reaper" in _DOMAIN_VALIDATORS


class TestAutonomousCliPolicyValidator:
    """reask 0 (= never re-ask) is legal; overrides map is validated."""

    def test_reask_zero_accepted(self):
        from genesis.mcp.health.settings import _validate_autonomous_cli_policy

        assert _validate_autonomous_cli_policy({"reask_interval_hours": 0}) == []

    def test_reask_negative_rejected(self):
        from genesis.mcp.health.settings import _validate_autonomous_cli_policy

        errs = _validate_autonomous_cli_policy({"reask_interval_hours": -1})
        assert errs and "between 0" in errs[0]

    def test_reask_overrides_valid_and_invalid(self):
        from genesis.mcp.health.settings import _validate_autonomous_cli_policy

        assert (
            _validate_autonomous_cli_policy(
                {"reask_overrides": {"inbox_evaluation": 0, "ego_cycle": 12}},
            )
            == []
        )
        errs = _validate_autonomous_cli_policy(
            {"reask_overrides": {"inbox_evaluation": 999}},
        )
        assert errs and "inbox_evaluation" in errs[0]
        errs2 = _validate_autonomous_cli_policy({"reask_overrides": "not-a-map"})
        assert errs2 and "mapping" in errs2[0]

    def test_fractional_and_bool_hours_rejected(self):
        """int(0.9) would silently truncate to the 0 = never sentinel —
        fractional and bool values must be rejected outright."""
        from genesis.mcp.health.settings import _validate_autonomous_cli_policy

        assert _validate_autonomous_cli_policy({"reask_interval_hours": 0.9})
        assert _validate_autonomous_cli_policy({"reask_interval_hours": True})
        assert _validate_autonomous_cli_policy(
            {"reask_overrides": {"inbox_evaluation": 0.5}},
        )
        assert _validate_autonomous_cli_policy({"reask_overrides": {"x": False}})


class TestSettingsProvenanceAndGateFlip:
    """2026-08-18 user directive: user-set config must never read as an
    anomaly to future sessions (durable provenance stamped by the WRITER),
    and disabling the mandatory approval gate needs explicit confirmation."""

    def test_atomic_write_stamps_and_preserves_provenance(self, tmp_path, monkeypatch):
        import genesis.mcp.health.settings as settings_mod

        monkeypatch.setattr(settings_mod, "_USER_CONFIG_DIR", tmp_path)
        p1 = settings_mod._atomic_yaml_write(
            "prov_test.yaml", {"a": 1}, provenance="user via dashboard PUT",
        )
        text1 = p1.read_text()
        assert text1.startswith("# set-by: user via dashboard PUT @ ")
        assert yaml.safe_load(text1) == {"a": 1}

        # A second write by a different actor PRESERVES the first stamp.
        p2 = settings_mod._atomic_yaml_write(
            "prov_test.yaml", {"a": 2}, provenance="user via mcp settings_update",
        )
        text2 = p2.read_text()
        assert "user via dashboard PUT" in text2
        assert "user via mcp settings_update" in text2
        assert yaml.safe_load(text2) == {"a": 2}

    @pytest.mark.asyncio
    async def test_gate_disable_requires_confirmation(self, tmp_path, monkeypatch):
        import genesis.mcp.health.settings as settings_mod
        from genesis.mcp.health.settings import _impl_settings_update

        monkeypatch.setattr(settings_mod, "_USER_CONFIG_DIR", tmp_path)
        result = await _impl_settings_update(
            "autonomous_cli_policy", {"manual_approval_required": False},
        )
        assert "error" in result
        assert "confirm_disable_approval_gate" in result["error"]
        assert not (tmp_path / "autonomous_cli_policy.local.yaml").exists()

        confirmed = await _impl_settings_update(
            "autonomous_cli_policy", {"manual_approval_required": False},
            confirm_disable_approval_gate=True,
        )
        assert confirmed.get("status") == "applied"
        assert "warning" in confirmed
        written = (tmp_path / "autonomous_cli_policy.local.yaml").read_text()
        assert "manual_approval_required: false" in written
        assert written.startswith("# set-by:")

    @pytest.mark.asyncio
    async def test_gate_enable_needs_no_confirmation(self, tmp_path, monkeypatch):
        import genesis.mcp.health.settings as settings_mod
        from genesis.mcp.health.settings import _impl_settings_update

        monkeypatch.setattr(settings_mod, "_USER_CONFIG_DIR", tmp_path)
        result = await _impl_settings_update(
            "autonomous_cli_policy", {"manual_approval_required": True},
        )
        assert result.get("status") == "applied"

    def test_hand_written_comments_survive_restamp(self, tmp_path, monkeypatch):
        """Audit lock: the WHOLE leading comment block survives a rewrite —
        hand-written operator rationale is exactly the record provenance
        stamping exists to protect; only machine set-by lines are capped."""
        import genesis.mcp.health.settings as settings_mod

        monkeypatch.setattr(settings_mod, "_USER_CONFIG_DIR", tmp_path)
        (tmp_path / "prov_mixed.yaml").write_text(
            "# set-by: user (dashboard PUT) @ 2026-08-18T13:44\n"
            "# provenance: deliberate sovereign choice — do NOT revert\n"
            "#   future sessions: this is user-intentional\n"
            "manual_approval_required: false\n",
        )
        p = settings_mod._atomic_yaml_write(
            "prov_mixed.yaml",
            {"manual_approval_required": False, "reask_interval_hours": 24},
            provenance="user via mcp settings_update",
        )
        text = p.read_text()
        assert "# provenance: deliberate sovereign choice" in text
        assert "future sessions: this is user-intentional" in text
        assert "# set-by: user (dashboard PUT) @ 2026-08-18T13:44" in text
        assert "user via mcp settings_update" in text
        assert yaml.safe_load(text) == {
            "manual_approval_required": False,
            "reask_interval_hours": 24,
        }

    @pytest.mark.asyncio
    async def test_gate_disable_falsy_nonbool_rejected_before_gate_check(
        self, tmp_path, monkeypatch,
    ):
        """Deep-review NOTE lock: manual_approval_required=0 (falsy but not
        False) must be rejected by the validator BEFORE it could slip past the
        `is False` gate check and be bool()-coerced to a silent disable."""
        import genesis.mcp.health.settings as settings_mod
        from genesis.mcp.health.settings import (
            _impl_settings_update,
            _validate_autonomous_cli_policy,
        )

        monkeypatch.setattr(settings_mod, "_USER_CONFIG_DIR", tmp_path)
        assert _validate_autonomous_cli_policy({"manual_approval_required": 0})
        assert _validate_autonomous_cli_policy({"manual_approval_required": None})
        result = await _impl_settings_update(
            "autonomous_cli_policy", {"manual_approval_required": 0},
        )
        assert result.get("error") == "validation failed"
        assert not (tmp_path / "autonomous_cli_policy.local.yaml").exists()

    def test_bare_write_preserves_existing_headers(self, tmp_path, monkeypatch):
        """Deep-review SHOULD-FIX lock: a caller that passes NO provenance
        (e.g. the dashboard surplus route pre-fix) must still preserve the
        existing header block — no caller may be a delete path."""
        import genesis.mcp.health.settings as settings_mod

        monkeypatch.setattr(settings_mod, "_USER_CONFIG_DIR", tmp_path)
        (tmp_path / "prov_bare.yaml").write_text(
            "# set-by: user via dashboard PUT @ 2026-08-18T13:44\n"
            "# provenance: hand-written rationale\n"
            "a: 1\n",
        )
        p = settings_mod._atomic_yaml_write("prov_bare.yaml", {"a": 2})
        text = p.read_text()
        assert "# set-by: user via dashboard PUT @ 2026-08-18T13:44" in text
        assert "# provenance: hand-written rationale" in text
        assert yaml.safe_load(text) == {"a": 2}

    def test_provenance_newlines_sanitized(self, tmp_path, monkeypatch):
        """Security lock: a newline in the actor string must never escape the
        comment prefix (raw top-level YAML above the real mapping = key
        injection that bypasses the gate-disable confirmation)."""
        import genesis.mcp.health.settings as settings_mod

        monkeypatch.setattr(settings_mod, "_USER_CONFIG_DIR", tmp_path)
        p = settings_mod._atomic_yaml_write(
            "prov_inject.yaml", {"a": 1},
            provenance="evil\nmanual_approval_required: false",
        )
        text = p.read_text()
        loaded = yaml.safe_load(text)
        assert loaded == {"a": 1}
        assert "manual_approval_required" not in loaded
        for line in text.splitlines():
            assert not line.strip() or line.startswith("#") or line.startswith("a:")

    @pytest.mark.asyncio
    async def test_gate_disable_alert_writes_and_reenable_resolves(
        self, tmp_path, monkeypatch,
    ):
        """The observation write must actually LAND (the original test never
        asserted it — the write silently failed into a stray db), and
        re-enabling the gate must resolve the standing alert (Codex P2)."""
        import aiosqlite

        import genesis.mcp.health.settings as settings_mod
        from genesis.db.schema import create_all_tables
        from genesis.mcp.health.settings import _impl_settings_update

        monkeypatch.setattr(settings_mod, "_USER_CONFIG_DIR", tmp_path)
        dbp = tmp_path / "obs.db"
        async with aiosqlite.connect(dbp) as db:
            db.row_factory = aiosqlite.Row
            await create_all_tables(db)
            await db.commit()
        monkeypatch.setattr("genesis.env.genesis_db_path", lambda: dbp)

        result = await _impl_settings_update(
            "autonomous_cli_policy", {"manual_approval_required": False},
            confirm_disable_approval_gate=True,
        )
        assert result.get("status") == "applied"
        async with aiosqlite.connect(dbp) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT resolved_at FROM observations WHERE source='settings_guard'",
            )
        assert len(rows) == 1 and rows[0]["resolved_at"] is None, (
            "the gate-disable critical observation must actually land"
        )

        result2 = await _impl_settings_update(
            "autonomous_cli_policy", {"manual_approval_required": True},
        )
        assert result2.get("status") == "applied"
        async with aiosqlite.connect(dbp) as db:
            db.row_factory = aiosqlite.Row
            rows = await db.execute_fetchall(
                "SELECT resolved_at FROM observations WHERE source='settings_guard' "
                "AND resolved_at IS NULL",
            )
        assert rows == [], "re-enable must resolve the standing alert"
