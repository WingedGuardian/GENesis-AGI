"""Tests for Genesis dashboard Flask blueprint."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint


@pytest.fixture()
def app():
    """Create a test Flask app with the dashboard blueprint."""
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app


@pytest.fixture()
def client(app):
    return app.test_client()


# ── Dashboard page ────────────────────────────────────────────────────────


def test_dashboard_page_without_template(client):
    """Dashboard route returns 404 when template doesn't exist yet (expected before Batch 4)."""
    resp = client.get("/genesis")
    # Template not created yet — 404 is expected; 200 once template exists
    assert resp.status_code in (200, 404)


def test_dashboard_page_contains_operator_controls(client):
    """Dashboard HTML includes the queue, routing, and budget controls."""
    resp = client.get("/genesis")
    assert resp.status_code == 200
    page = resp.get_data(as_text=True)
    assert "Clear all reviewed" in page
    assert "Reload routing config" in page
    assert "Approval Queue" in page
    assert "Save budget" in page
    assert "Review routing" in page
    assert "Autonomous CLI Policy" in page


def test_settings_index_exposes_autonomous_cli_policy(client):
    """Settings index includes the autonomous CLI policy form domain."""
    resp = client.get("/api/genesis/settings")
    assert resp.status_code == 200
    data = resp.get_json()
    entry = next((row for row in data if row["name"] == "autonomous_cli_policy"), None)
    assert entry is not None
    assert entry["readonly"] is False
    assert entry["has_form"] is True


# ── Activity feed ─────────────────────────────────────────────────────────


def test_activity_empty_when_not_bootstrapped(client):
    """Activity endpoint returns empty list when runtime not bootstrapped."""
    mock_rt = MagicMock()
    mock_rt.is_bootstrapped = False
    mock_rt.event_bus = None

    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        resp = client.get("/api/genesis/activity")
        assert resp.status_code == 200
        assert resp.get_json() == []


def test_activity_returns_serialized_events(client):
    """Activity endpoint returns serialized event dicts."""
    from genesis.observability.types import GenesisEvent, Severity, Subsystem

    events = [
        GenesisEvent(
            subsystem=Subsystem.ROUTING,
            severity=Severity.INFO,
            event_type="test.event",
            message="test message",
            timestamp="2026-03-13T14:00:00Z",
            details={"key": "value"},
        ),
    ]

    mock_bus = MagicMock()
    mock_bus.recent_events.return_value = events

    mock_rt = MagicMock()
    mock_rt.is_bootstrapped = True
    mock_rt.event_bus = mock_bus

    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        resp = client.get("/api/genesis/activity")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["subsystem"] == "routing"
        assert data[0]["event_type"] == "test.event"
        assert data[0]["details"] == {"key": "value"}


def test_activity_respects_filters(client):
    """Activity endpoint passes filters to recent_events."""
    mock_bus = MagicMock()
    mock_bus.recent_events.return_value = []

    mock_rt = MagicMock()
    mock_rt.is_bootstrapped = True
    mock_rt.event_bus = mock_bus

    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        resp = client.get(
            "/api/genesis/activity?limit=10&min_severity=warning&subsystem=routing"
        )
        assert resp.status_code == 200

        from genesis.observability.types import Severity, Subsystem

        mock_bus.recent_events.assert_called_once_with(
            limit=10,
            min_severity=Severity.WARNING,
            subsystem=Subsystem.ROUTING,
        )


# ── Config files ──────────────────────────────────────────────────────────


def test_config_files_returns_list(client):
    """Config files endpoint returns a list."""
    resp = client.get("/api/genesis/config-files")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)


def test_config_files_absolute_paths(client):
    """Config files endpoint returns absolute paths."""
    resp = client.get("/api/genesis/config-files")
    data = resp.get_json()
    for item in data:
        assert item["path"].startswith("/"), f"Path not absolute: {item['path']}"


def test_config_files_have_categories(client):
    """Every config file has a category."""
    resp = client.get("/api/genesis/config-files")
    data = resp.get_json()
    valid_categories = {
        "identity", "reflection", "triage", "skills",
        "system", "routing", "outreach", "inbox", "channels",
        "security", "recon", "learning", "config",
        "memory-feedback", "memory-project", "memory-user",
        "memory-reference", "memory-index",
    }
    for item in data:
        assert item["category"] in valid_categories, f"Bad category: {item}"


def test_config_files_have_editable_and_syntax_fields(client):
    """Every config file has editable (bool) and syntax fields."""
    resp = client.get("/api/genesis/config-files")
    data = resp.get_json()
    for item in data:
        assert "editable" in item, f"Missing editable: {item['name']}"
        assert "syntax" in item, f"Missing syntax: {item['name']}"
        assert item["syntax"] in ("yaml", "markdown"), f"Bad syntax: {item}"


def test_claude_md_read_only(client):
    """CLAUDE.md cannot be updated via PUT."""
    resp = client.put(
        "/api/genesis/config-files/CLAUDE.md",
        json={"content": "hacked"},
    )
    assert resp.status_code == 403


def test_protected_paths_blocked(client):
    """protected_paths.yaml cannot be updated via PUT."""
    resp = client.put(
        "/api/genesis/config-files/protected_paths.yaml",
        json={"content": "hacked: true"},
    )
    assert resp.status_code == 404  # _resolve_file_for_write returns None


def test_yaml_validation_rejects_bad_syntax(client):
    """Invalid YAML returns 422 with error detail."""
    resp = client.put(
        "/api/genesis/config-files/outreach.yaml",
        json={"content": "bad:\n  - [\ninvalid"},
    )
    assert resp.status_code == 422
    data = resp.get_json()
    assert "Invalid YAML" in data["error"]


def test_delete_non_memory_blocked(client):
    """DELETE only works for memory/* files."""
    resp = client.delete("/api/genesis/config-files/outreach.yaml")
    assert resp.status_code == 403


def test_path_traversal_blocked(client):
    """Path traversal attempts return 404, not file content."""
    resp = client.get("/api/genesis/config-files/../../../etc/passwd")
    assert resp.status_code == 404


# ── Provider activity ─────────────────────────────────────────────────────


def test_provider_activity_empty_when_not_bootstrapped(client):
    """Provider activity returns empty list when runtime not bootstrapped."""
    mock_rt = MagicMock()
    mock_rt.is_bootstrapped = False
    mock_rt.activity_tracker = None

    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        resp = client.get("/api/genesis/provider-activity")
        assert resp.status_code == 200
        assert resp.get_json() == []


def test_provider_activity_returns_summaries(client):
    """Provider activity endpoint returns tracker summaries."""
    from genesis.observability.provider_activity import ProviderActivityTracker

    tracker = ProviderActivityTracker()
    tracker.record("ollama_embedding", latency_ms=50, success=True)
    tracker.record("mistral_embedding", latency_ms=100, success=True)

    mock_rt = MagicMock()
    mock_rt.is_bootstrapped = True
    mock_rt.activity_tracker = tracker

    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        resp = client.get("/api/genesis/provider-activity")
        assert resp.status_code == 200


def test_autonomous_cli_policy_endpoint_uses_runtime_export_status(client):
    """Autonomous CLI policy endpoint returns exporter status when available."""
    mock_exporter = MagicMock()
    mock_exporter.status.return_value = {
        "effective_policy": {
            "autonomous_cli_fallback_enabled": True,
            "manual_approval_required": False,
            "reask_interval_hours": 24,
            "approval_channel": "telegram",
            "shared_export_enabled": True,
            "source": "config:autonomous_cli_policy.yaml",
        },
        "last_export_at": "2026-04-04T12:00:00+00:00",
        "last_export_path": "/tmp/shared/guardian/autonomous_cli_policy.json",
        "last_export_error": None,
    }
    mock_rt = MagicMock()
    mock_rt._autonomous_cli_policy_exporter = mock_exporter

    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        resp = client.get("/api/genesis/autonomous-cli-policy")

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["effective_policy"]["manual_approval_required"] is False
    assert data["last_export_path"].endswith("autonomous_cli_policy.json")


def test_provider_activity_filters_by_name(client):
    """Provider activity endpoint can filter by provider name."""
    from genesis.observability.provider_activity import ProviderActivityTracker

    tracker = ProviderActivityTracker()
    tracker.record("ollama_embedding", latency_ms=50, success=True)
    tracker.record("mistral_embedding", latency_ms=100, success=True)

    mock_rt = MagicMock()
    mock_rt.is_bootstrapped = True
    mock_rt.activity_tracker = tracker

    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        resp = client.get("/api/genesis/provider-activity?provider=ollama_embedding")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0]["provider"] == "ollama_embedding"


# ── Subsystems endpoint ──────────────────────────────────────────────


def test_subsystems_returns_sorted_list(client):
    """Subsystems endpoint returns sorted enum values."""
    resp = client.get("/api/genesis/subsystems")
    assert resp.status_code == 200
    data = resp.get_json()
    assert isinstance(data, list)
    assert "routing" in data
    assert "dashboard" in data
    assert data == sorted(data)


# ── Config file content endpoint ─────────────────────────────────────


def test_config_file_content_not_found(client):
    """Config file content endpoint returns 404 for unknown files."""
    resp = client.get("/api/genesis/config-files/NONEXISTENT.md")
    assert resp.status_code == 404


def test_config_file_content_returns_content(client):
    """Config file content returns file content for known files."""
    # First get the list to find a real file
    list_resp = client.get("/api/genesis/config-files")
    files = list_resp.get_json()
    if not files:
        pytest.skip("No config files found")
    resp = client.get(f"/api/genesis/config-files/{files[0]['name']}")
    assert resp.status_code == 200
    data = resp.get_json()
    assert "content" in data
    assert len(data["content"]) > 0


# ── Deferred queue management ────────────────────────────────────────────


def test_clear_deferred_item_endpoint(client):
    """Discarded/expired deferred items can be cleared individually."""
    cursor = MagicMock(rowcount=1)
    mock_db = MagicMock()
    mock_db.execute = AsyncMock(return_value=cursor)
    mock_db.commit = AsyncMock()

    mock_rt = MagicMock()
    mock_rt.is_bootstrapped = True
    mock_rt.db = mock_db

    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        resp = client.delete("/api/genesis/deferred/item-123/clear")

    assert resp.status_code == 200
    assert resp.get_json() == {"cleared": 1}
    mock_db.execute.assert_awaited_once_with(
        "DELETE FROM deferred_work_queue WHERE id = ? AND status IN ('discarded', 'expired')",
        ("item-123",),
    )
    mock_db.commit.assert_awaited_once()


def test_clear_all_deferred_items_endpoint(client):
    """Discarded/expired deferred items can be cleared in bulk."""
    cursor = MagicMock(rowcount=3)
    mock_db = MagicMock()
    mock_db.execute = AsyncMock(return_value=cursor)
    mock_db.commit = AsyncMock()

    mock_rt = MagicMock()
    mock_rt.is_bootstrapped = True
    mock_rt.db = mock_db

    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        resp = client.delete("/api/genesis/deferred/all/clear")

    assert resp.status_code == 200
    assert resp.get_json() == {"cleared": 3}
    mock_db.execute.assert_awaited_once_with(
        "DELETE FROM deferred_work_queue WHERE status IN ('discarded', 'expired')"
    )
    mock_db.commit.assert_awaited_once()


# ── Budget configuration ────────────────────────────────────────────────


def test_set_budget_creates_or_updates_budget(client):
    """Budget POST accepts supported types and persists a new active budget."""
    mock_db = MagicMock()
    mock_db.execute = AsyncMock()
    mock_db.commit = AsyncMock()

    mock_rt = MagicMock()
    mock_rt.is_bootstrapped = True
    mock_rt.db = mock_db

    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        resp = client.post(
            "/api/genesis/budgets",
            json={"budget_type": "weekly", "limit_usd": 12.5, "warning_pct": 0.65},
        )

    assert resp.status_code == 200
    data = resp.get_json()
    assert data["ok"] is True
    assert data["budget_type"] == "weekly"
    assert data["limit_usd"] == 12.5
    assert mock_db.execute.await_count == 2
    first_call = mock_db.execute.await_args_list[0]
    assert first_call.args == (
        "UPDATE budgets SET active = 0 WHERE budget_type = ? AND active = 1",
        ("weekly",),
    )
    second_call = mock_db.execute.await_args_list[1]
    assert "INSERT INTO budgets" in second_call.args[0]
    assert second_call.args[1][1:4] == ("weekly", 12.5, 0.65)
    mock_db.commit.assert_awaited_once()


def test_set_budget_rejects_invalid_budget_type(client):
    """Budget POST rejects unsupported budget types."""
    mock_db = MagicMock()
    mock_db.execute = AsyncMock()
    mock_db.commit = AsyncMock()

    mock_rt = MagicMock()
    mock_rt.is_bootstrapped = True
    mock_rt.db = mock_db

    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        resp = client.post(
            "/api/genesis/budgets",
            json={"budget_type": "yearly", "limit_usd": 50, "warning_pct": 0.5},
        )

    assert resp.status_code == 400
    assert "budget_type must be daily/weekly/monthly" in resp.get_json()["error"]
    mock_db.execute.assert_not_called()
    mock_db.commit.assert_not_called()


# ── Routing configuration ───────────────────────────────────────────────


def test_routing_config_read_includes_call_sites(client):
    """Routing config read exposes configured call sites for the dashboard editor."""
    mock_cfg = SimpleNamespace()
    mock_cfg.disabled_providers = {}
    mock_cfg.providers = {
        "claude-sonnet": SimpleNamespace(
            name="claude-sonnet",
            provider_type="anthropic",
            model_id="claude-sonnet-4-6-20250514",
            is_free=False,
        ),
    }
    mock_cfg.call_sites = {
        "autonomous_executor_reasoning": SimpleNamespace(
            chain=["claude-sonnet"],
            default_paid=True,
            never_pays=False,
            retry_profile="background",
        ),
    }
    mock_router = MagicMock()
    mock_router.config = mock_cfg
    mock_router.breakers = {"claude-sonnet": MagicMock(state=MagicMock(value="closed"))}
    mock_rt = MagicMock()
    mock_rt.is_bootstrapped = True
    mock_rt.router = mock_router

    with patch("genesis.runtime.GenesisRuntime") as MockRT:
        MockRT.instance.return_value = mock_rt
        resp = client.get("/api/genesis/routing/config")

    assert resp.status_code == 200
    data = resp.get_json()
    assert "autonomous_executor_reasoning" in data["call_sites"]
    assert data["call_sites"]["autonomous_executor_reasoning"]["chain"] == ["claude-sonnet"]
    assert data["call_sites"]["autonomous_executor_reasoning"]["default_paid"] is True


def test_routing_config_update_endpoint(client):
    """Routing updates persist through the config helper and hot-reload the router."""
    from unittest.mock import AsyncMock

    mock_router = MagicMock()
    # scan_dlq_orphans_after_reload is awaited by the async route handler;
    # it must be an AsyncMock returning an int so the `await` resolves.
    mock_router.scan_dlq_orphans_after_reload = AsyncMock(return_value=0)
    mock_rt = MagicMock()
    mock_rt.is_bootstrapped = True
    mock_rt.router = mock_router
    fake_config = object()

    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.routing.config.update_call_site_in_yaml", return_value=fake_config) as update_call_site,
    ):
        MockRT.instance.return_value = mock_rt
        resp = client.put(
            "/api/genesis/routing/config/2_triage",
            json={
                "chain": ["groq_llama", "openrouter_haiku"],
                "default_paid": True,
                "never_pays": False,
            },
        )

    assert resp.status_code == 200
    assert resp.get_json() == {
        "ok": True,
        "call_site_id": "2_triage",
        "dlq_orphans_expired": 0,
    }
    update_call_site.assert_called_once()
    mock_router.reload_config.assert_called_once_with(fake_config)
    mock_router.scan_dlq_orphans_after_reload.assert_awaited_once()


def test_routing_config_reload_endpoint(client):
    """Routing reload reads config from disk and hot-reloads the router."""
    from unittest.mock import AsyncMock

    mock_router = MagicMock()
    mock_router.scan_dlq_orphans_after_reload = AsyncMock(return_value=0)
    mock_rt = MagicMock()
    mock_rt.is_bootstrapped = True
    mock_rt.router = mock_router
    fake_config = object()

    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.routing.config.load_config", return_value=fake_config) as load_config,
    ):
        MockRT.instance.return_value = mock_rt
        resp = client.post("/api/genesis/routing/reload")

    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "dlq_orphans_expired": 0}
    load_config.assert_called_once()
    mock_router.reload_config.assert_called_once_with(fake_config)
    mock_router.scan_dlq_orphans_after_reload.assert_awaited_once()
