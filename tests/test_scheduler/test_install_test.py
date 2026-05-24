"""Tests for install test runner and VM provider."""

from __future__ import annotations

import pytest

from genesis.scheduler.install_test import classify_error
from genesis.scheduler.vm_provider import (
    AWSProvider,
    AzureProvider,
    GCPProvider,
    VMInstance,
    get_provider,
)


class TestClassifyError:
    """Error classification for install output."""

    def test_structural_module_not_found(self) -> None:
        assert classify_error("ModuleNotFoundError: no module named 'foo'") == "structural"

    def test_structural_command_not_found(self) -> None:
        assert classify_error("bash: ruff: command not found") == "structural"

    def test_structural_import_error(self) -> None:
        assert classify_error("ImportError: cannot import name 'x'") == "structural"

    def test_structural_permission_denied(self) -> None:
        assert classify_error("Permission denied (publickey)") == "structural"

    def test_transient_dns(self) -> None:
        assert classify_error("Could not resolve host github.com") == "transient"

    def test_transient_timeout(self) -> None:
        assert classify_error("Connection timed out after 30s") == "transient"

    def test_transient_apt_mirror(self) -> None:
        assert classify_error("Unable to fetch some archives, maybe run apt-get update") == "transient"

    def test_unknown_generic(self) -> None:
        assert classify_error("Something else went wrong") == "unknown"

    def test_empty_output(self) -> None:
        assert classify_error("") == "unknown"


class TestVMProviderRegistry:
    """Provider registry and stub behavior."""

    def test_get_aws_provider(self) -> None:
        p = get_provider("aws")
        assert isinstance(p, AWSProvider)
        assert p.name == "aws"

    def test_get_azure_provider(self) -> None:
        p = get_provider("azure")
        assert isinstance(p, AzureProvider)
        assert p.name == "azure"

    def test_get_gcp_provider(self) -> None:
        p = get_provider("gcp")
        assert isinstance(p, GCPProvider)
        assert p.name == "gcp"

    def test_unknown_provider_raises(self) -> None:
        with pytest.raises(KeyError, match="Unknown VM provider"):
            get_provider("digitalocean")

    @pytest.mark.asyncio
    async def test_aws_provision_not_implemented(self) -> None:
        p = AWSProvider()
        with pytest.raises(NotImplementedError, match="AWS provider"):
            await p.provision({})

    @pytest.mark.asyncio
    async def test_azure_provision_not_implemented(self) -> None:
        p = AzureProvider()
        with pytest.raises(NotImplementedError, match="Azure provider"):
            await p.provision({})

    @pytest.mark.asyncio
    async def test_gcp_provision_not_implemented(self) -> None:
        p = GCPProvider()
        with pytest.raises(NotImplementedError, match="GCP provider"):
            await p.provision({})


class TestVMInstance:
    """VMInstance dataclass."""

    def test_defaults(self) -> None:
        vm = VMInstance(instance_id="i-123", provider="aws", ip="1.2.3.4")
        assert vm.ssh_user == "ubuntu"
        assert vm.region == ""
        assert vm.metadata == {}

    def test_custom_fields(self) -> None:
        vm = VMInstance(
            instance_id="i-456",
            provider="gcp",
            ip="10.0.0.1",
            ssh_user="admin",
            region="us-central1-a",
            metadata={"zone": "a"},
        )
        assert vm.provider == "gcp"
        assert vm.ssh_user == "admin"
        assert vm.metadata["zone"] == "a"


@pytest.mark.asyncio
async def test_run_install_test_not_implemented():
    """Running with a stub provider returns config error."""
    from genesis.scheduler.install_test import run_install_test

    result = await run_install_test({"provider": "aws"})
    assert not result["success"]
    assert result["error_type"] == "config"
    assert "not yet implemented" in result["error_message"].lower()
