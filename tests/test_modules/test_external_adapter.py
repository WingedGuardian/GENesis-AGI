"""Tests for genesis.modules.external — ExternalProgramAdapter + IPC."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from genesis.modules.base import CapabilityModule
from genesis.modules.external.adapter import ExternalProgramAdapter
from genesis.modules.external.config import (
    IPCConfig,
    ProgramConfig,
)
from genesis.modules.external.ipc import HttpIPCAdapter, StdioIPCAdapter, create_ipc_adapter

# ---------------------------------------------------------------------------
# ProgramConfig
# ---------------------------------------------------------------------------


class TestProgramConfig:
    def test_from_dict_minimal(self):
        cfg = ProgramConfig.from_dict({"name": "test-prog"})
        assert cfg.name == "test-prog"
        assert cfg.ipc.method == "http"
        assert cfg.health_check is None
        assert cfg.lifecycle is None
        assert cfg.research_profile is None
        assert cfg.enabled is False

    def test_from_dict_full(self):
        data = {
            "name": "Test Agent",
            "description": "Test automation tool",
            "ipc": {
                "method": "http",
                "url": "http://example-host:8080",
                "timeout": 15,
            },
            "health_check": {
                "endpoint": "/api/health",
                "interval_seconds": 30,
                "expected_status": 200,
            },
            "lifecycle": {
                "ssh_host": "user@host",
                "source_dir": "/home/user/app",
                "restart_cmd": "systemctl restart app",
            },
            "research_profile": "test-ops",
            "enabled": True,
            "configurable": {"max_jobs": 100},
        }
        cfg = ProgramConfig.from_dict(data)
        assert cfg.name == "Test Agent"
        assert cfg.ipc.url == "http://example-host:8080"
        assert cfg.ipc.timeout == 15
        assert cfg.health_check.endpoint == "/api/health"
        assert cfg.health_check.interval_seconds == 30
        assert cfg.lifecycle.ssh_host == "user@host"
        assert cfg.lifecycle.restart_cmd == "systemctl restart app"
        assert cfg.research_profile == "test-ops"
        assert cfg.enabled is True
        assert cfg.configurable["max_jobs"] == 100

    def test_from_dict_stdio(self):
        data = {
            "name": "local-tool",
            "ipc": {
                "method": "stdio",
                "command": ["python", "tool.py"],
                "working_dir": "/tmp",
            },
        }
        cfg = ProgramConfig.from_dict(data)
        assert cfg.ipc.method == "stdio"
        assert cfg.ipc.command == ["python", "tool.py"]
        assert cfg.ipc.working_dir == Path("/tmp")


# ---------------------------------------------------------------------------
# IPC Factory
# ---------------------------------------------------------------------------


class TestIPCFactory:
    def test_create_http(self):
        config = IPCConfig(method="http", url="http://localhost:8000")
        adapter = create_ipc_adapter(config)
        assert isinstance(adapter, HttpIPCAdapter)

    def test_create_stdio(self):
        config = IPCConfig(method="stdio", command=["echo", "hello"])
        adapter = create_ipc_adapter(config)
        assert isinstance(adapter, StdioIPCAdapter)

    def test_create_unknown_raises(self):
        config = IPCConfig(method="unknown")
        with pytest.raises(ValueError, match="Unknown IPC method"):
            create_ipc_adapter(config)


# ---------------------------------------------------------------------------
# HttpIPCAdapter
# ---------------------------------------------------------------------------


class TestHttpIPCAdapter:
    def test_requires_url(self):
        with pytest.raises(ValueError, match="requires a url"):
            HttpIPCAdapter(IPCConfig(method="http"))

    @pytest.mark.asyncio
    async def test_send_get(self):
        adapter = HttpIPCAdapter(IPCConfig(method="http", url="http://test:8000"))
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"status": "ok"}
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        adapter._client = mock_client

        result = await adapter.send("/api/test")
        assert result == {"status": "ok"}
        mock_client.get.assert_called_once_with("/api/test", params=None)

    @pytest.mark.asyncio
    async def test_send_post(self):
        adapter = HttpIPCAdapter(IPCConfig(method="http", url="http://test:8000"))
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"created": True}
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        adapter._client = mock_client

        result = await adapter.send("/api/create", data={"name": "test"}, method="POST")
        assert result == {"created": True}

    @pytest.mark.asyncio
    async def test_health_check_success(self):
        adapter = HttpIPCAdapter(IPCConfig(method="http", url="http://test:8000"))
        mock_resp = AsyncMock()
        mock_resp.status_code = 200

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        adapter._client = mock_client

        assert await adapter.health_check("/health", 200) is True

    @pytest.mark.asyncio
    async def test_health_check_failure(self):
        adapter = HttpIPCAdapter(IPCConfig(method="http", url="http://test:8000"))
        mock_resp = AsyncMock()
        mock_resp.status_code = 503

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        adapter._client = mock_client

        assert await adapter.health_check("/health", 200) is False

    @pytest.mark.asyncio
    async def test_send_not_started_raises(self):
        adapter = HttpIPCAdapter(IPCConfig(method="http", url="http://test:8000"))
        with pytest.raises(RuntimeError, match="not started"):
            await adapter.send("/test")


# ---------------------------------------------------------------------------
# StdioIPCAdapter
# ---------------------------------------------------------------------------


class TestStdioIPCAdapter:
    def test_requires_command(self):
        with pytest.raises(ValueError, match="requires a command"):
            StdioIPCAdapter(IPCConfig(method="stdio"))


# ---------------------------------------------------------------------------
# ExternalProgramAdapter (CapabilityModule protocol)
# ---------------------------------------------------------------------------


class TestExternalProgramAdapter:
    def _make_adapter(self, **overrides):
        data = {
            "name": "Test Program",
            "ipc": {"method": "http", "url": "http://localhost:9999"},
            "health_check": {"endpoint": "/health"},
            "enabled": True,
        }
        data.update(overrides)
        config = ProgramConfig.from_dict(data)
        return ExternalProgramAdapter(config)

    def test_satisfies_protocol(self):
        adapter = self._make_adapter()
        assert isinstance(adapter, CapabilityModule)

    def test_name(self):
        adapter = self._make_adapter(name="My Tool")
        assert adapter.name == "My Tool"

    def test_enabled_default(self):
        adapter = self._make_adapter(enabled=True)
        assert adapter.enabled is True

    def test_enabled_setter(self):
        adapter = self._make_adapter(enabled=False)
        assert adapter.enabled is False
        adapter.enabled = True
        assert adapter.enabled is True

    def test_research_profile(self):
        adapter = self._make_adapter(research_profile="test-profile")
        assert adapter.get_research_profile_name() == "test-profile"

    def test_research_profile_none(self):
        adapter = self._make_adapter()
        assert adapter.get_research_profile_name() is None

    @pytest.mark.asyncio
    async def test_register_healthy(self):
        adapter = self._make_adapter()
        adapter._ipc = AsyncMock()
        adapter._ipc.start = AsyncMock()
        adapter._ipc.health_check = AsyncMock(return_value=True)

        await adapter.register(None)
        assert adapter.healthy is True
        adapter._ipc.start.assert_called_once()
        adapter._ipc.health_check.assert_called_once_with("/health", 200)

    @pytest.mark.asyncio
    async def test_register_unhealthy(self):
        adapter = self._make_adapter()
        adapter._ipc = AsyncMock()
        adapter._ipc.start = AsyncMock()
        adapter._ipc.health_check = AsyncMock(return_value=False)

        await adapter.register(None)
        assert adapter.healthy is False

    @pytest.mark.asyncio
    async def test_deregister(self):
        adapter = self._make_adapter()
        adapter._ipc = AsyncMock()
        adapter._healthy = True

        await adapter.deregister()
        adapter._ipc.stop.assert_called_once()
        assert adapter.healthy is False

    @pytest.mark.asyncio
    async def test_handle_opportunity_healthy(self):
        adapter = self._make_adapter()
        adapter._healthy = True
        adapter._ipc = AsyncMock()
        adapter._ipc.send = AsyncMock(return_value={"action": "apply"})

        result = await adapter.handle_opportunity({"signal": "test"})
        assert result == {"action": "apply"}

    @pytest.mark.asyncio
    async def test_handle_opportunity_unhealthy(self):
        adapter = self._make_adapter()
        adapter._healthy = False

        result = await adapter.handle_opportunity({"signal": "test"})
        assert result is None

    @pytest.mark.asyncio
    async def test_extract_generalizable_returns_none(self):
        adapter = self._make_adapter()
        assert await adapter.extract_generalizable({}) is None

    def test_configurable_fields(self):
        adapter = self._make_adapter(configurable={"max_jobs": 100, "auto_apply": True})
        fields = adapter.configurable_fields()
        assert len(fields) == 2
        names = {f["name"] for f in fields}
        assert "max_jobs" in names
        assert "auto_apply" in names

    def test_update_config(self):
        adapter = self._make_adapter(configurable={"max_jobs": 100})
        result = adapter.update_config({"max_jobs": 200})
        assert result["max_jobs"] == 200

    @pytest.mark.asyncio
    async def test_check_health(self):
        adapter = self._make_adapter()
        adapter._ipc = AsyncMock()
        adapter._ipc.health_check = AsyncMock(return_value=True)
        adapter._healthy = False

        assert await adapter.check_health() is True
        assert adapter.healthy is True


# ---------------------------------------------------------------------------
# YAML auto-discovery integration
# ---------------------------------------------------------------------------


class TestExternalModuleDiscovery:
    @pytest.mark.asyncio
    async def test_load_from_yaml(self, tmp_path):
        """External module YAML is discovered and loaded into registry."""
        import yaml

        from genesis.modules.registry import ModuleRegistry

        config_dir = tmp_path / "config" / "external-modules"
        config_dir.mkdir(parents=True)

        yaml_content = {
            "name": "test-external",
            "ipc": {"method": "http", "url": "http://localhost:1234"},
            "enabled": False,
        }
        (config_dir / "test.yaml").write_text(yaml.dump(yaml_content))

        registry = ModuleRegistry()

        # Simulate what _load_external_modules does
        from genesis.modules.external.adapter import ExternalProgramAdapter
        from genesis.modules.external.config import ProgramConfig

        for yaml_path in config_dir.glob("*.yaml"):
            data = yaml.safe_load(yaml_path.read_text())
            config = ProgramConfig.from_dict(data)
            adapter = ExternalProgramAdapter(config)
            await registry.load_module(adapter)

        assert "test-external" in registry.list_modules()
        mod = registry.get("test-external")
        assert isinstance(mod, ExternalProgramAdapter)
        assert mod.enabled is False
