"""Tests for install test runner and VM provider."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from genesis.scheduler.install_test import classify_error
from genesis.scheduler.vm_provider import (
    AWSProvider,
    AzureProvider,
    GCPProvider,
    VMInstance,
    _run_ssh,
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


# ─── AWS Provider Tests (mocked boto3) ──────────────────────────────────────


class TestAWSProvider:
    """AWS provider with mocked boto3 — tests the orchestration logic."""

    def _make_provider(self) -> AWSProvider:
        return AWSProvider()

    @pytest.mark.asyncio
    async def test_resolve_ami(self) -> None:
        provider = self._make_provider()
        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "ami-0abcdef1234567890"},
        }
        with patch.object(provider, "_get_ssm_client", return_value=mock_ssm):
            ami = await provider._resolve_ami("us-east-1")
        assert ami == "ami-0abcdef1234567890"

    @pytest.mark.asyncio
    async def test_ensure_security_group_existing(self) -> None:
        provider = self._make_provider()
        mock_client = MagicMock()
        mock_client.describe_security_groups.return_value = {
            "SecurityGroups": [{"GroupId": "sg-existing123"}],
        }
        with patch.object(provider, "_get_ec2_client", return_value=mock_client):
            sg_id = await provider._ensure_security_group("us-east-1")
        assert sg_id == "sg-existing123"
        mock_client.create_security_group.assert_not_called()

    @pytest.mark.asyncio
    async def test_ensure_security_group_creates_new(self) -> None:
        provider = self._make_provider()
        mock_client = MagicMock()
        mock_client.describe_security_groups.return_value = {"SecurityGroups": []}
        mock_client.create_security_group.return_value = {"GroupId": "sg-new456"}
        with patch.object(provider, "_get_ec2_client", return_value=mock_client):
            sg_id = await provider._ensure_security_group("us-east-1")
        assert sg_id == "sg-new456"
        mock_client.authorize_security_group_ingress.assert_called_once()
        mock_client.create_tags.assert_called_once()

    @pytest.mark.asyncio
    async def test_create_key_pair(self, tmp_path) -> None:
        provider = self._make_provider()
        mock_client = MagicMock()
        mock_client.create_key_pair.return_value = {
            "KeyMaterial": "-----BEGIN OPENSSH PRIVATE KEY-----\nfake\n-----END OPENSSH PRIVATE KEY-----",
        }
        with (
            patch.object(provider, "_get_ec2_client", return_value=mock_client),
            patch("genesis.scheduler.vm_provider._KEY_DIR", tmp_path),
        ):
            key_name, key_path = await provider._create_key_pair("us-east-1", "abc123")
        assert key_name == "genesis-install-test-abc123"
        assert "abc123" in key_path
        import os
        assert os.path.exists(key_path)
        # Key file should be 0600
        assert oct(os.stat(key_path).st_mode)[-3:] == "600"

    @pytest.mark.asyncio
    async def test_provision_full_flow(self, tmp_path) -> None:
        """Test the full provision flow with all boto3 calls mocked."""
        provider = self._make_provider()

        # Mock SSM
        mock_ssm = MagicMock()
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "ami-test123"},
        }

        # Mock EC2 client
        mock_client = MagicMock()
        mock_client.describe_security_groups.return_value = {
            "SecurityGroups": [{"GroupId": "sg-test"}],
        }
        mock_client.create_key_pair.return_value = {
            "KeyMaterial": "fake-key-material",
        }

        # Mock EC2 resource
        mock_instance = MagicMock()
        mock_instance.id = "i-test789"
        mock_instance.public_ip_address = "54.1.2.3"
        mock_instance.wait_until_running = MagicMock()
        mock_instance.reload = MagicMock()

        mock_ec2 = MagicMock()
        mock_ec2.create_instances.return_value = [mock_instance]

        with (
            patch.object(provider, "_get_ssm_client", return_value=mock_ssm),
            patch.object(provider, "_get_ec2_client", return_value=mock_client),
            patch.object(provider, "_get_ec2", return_value=mock_ec2),
            patch("genesis.scheduler.vm_provider._KEY_DIR", tmp_path),
        ):
            vm = await provider.provision({"region": "us-east-1"})

        assert vm.instance_id == "i-test789"
        assert vm.ip == "54.1.2.3"
        assert vm.provider == "aws"
        assert vm.region == "us-east-1"
        assert vm.metadata["sg_id"] == "sg-test"
        mock_ec2.create_instances.assert_called_once()

    @pytest.mark.asyncio
    async def test_teardown(self) -> None:
        provider = self._make_provider()
        mock_client = MagicMock()
        vm = VMInstance(
            instance_id="i-teardown",
            provider="aws",
            ip="1.2.3.4",
            region="us-east-1",
            ssh_key_path="/tmp/fake.pem",
            metadata={"key_name": "genesis-install-test-xyz"},
        )
        with patch.object(provider, "_get_ec2_client", return_value=mock_client):
            await provider.teardown(vm)
        mock_client.terminate_instances.assert_called_once_with(
            InstanceIds=["i-teardown"],
        )
        mock_client.delete_key_pair.assert_called_once_with(
            KeyName="genesis-install-test-xyz",
        )

    @pytest.mark.asyncio
    async def test_teardown_idempotent(self) -> None:
        """Teardown doesn't raise if instance is already terminated."""
        provider = self._make_provider()
        mock_client = MagicMock()
        mock_client.terminate_instances.side_effect = Exception("already terminated")
        vm = VMInstance(
            instance_id="i-gone",
            provider="aws",
            ip="1.2.3.4",
            region="us-east-1",
            metadata={},
        )
        with patch.object(provider, "_get_ec2_client", return_value=mock_client):
            # Should not raise
            await provider.teardown(vm)

    @pytest.mark.asyncio
    async def test_ssh_command_delegates(self) -> None:
        provider = self._make_provider()
        vm = VMInstance(
            instance_id="i-ssh",
            provider="aws",
            ip="10.0.0.1",
            ssh_key_path="/tmp/key.pem",
        )
        with patch("genesis.scheduler.vm_provider._run_ssh", new_callable=AsyncMock) as mock_ssh:
            mock_ssh.return_value = "hello"
            result = await provider.ssh_command(vm, "echo hello")
        assert result == "hello"
        mock_ssh.assert_called_once_with("10.0.0.1", "ubuntu", "/tmp/key.pem", "echo hello")


# ─── SSH Helper Tests ───────────────────────────────────────────────────────


class TestRunSSH:
    """Test the _run_ssh helper."""

    @pytest.mark.asyncio
    async def test_success(self) -> None:
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"output", b"")
        mock_proc.returncode = 0
        mock_proc.kill = MagicMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
            result = await _run_ssh("1.2.3.4", "ubuntu", "/key.pem", "echo hi")
        assert result == "output"

    @pytest.mark.asyncio
    async def test_failure_raises(self) -> None:
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"Permission denied")
        mock_proc.returncode = 255
        mock_proc.kill = MagicMock()

        with patch("asyncio.create_subprocess_exec", return_value=mock_proc), \
             pytest.raises(RuntimeError, match="SSH command failed"):
            await _run_ssh("1.2.3.4", "ubuntu", "/key.pem", "whoami")


# ─── Integration test (install_test.py with mocked AWS) ────────────────────


@pytest.mark.asyncio
async def test_run_install_test_with_mocked_aws():
    """Full install test cycle with mocked AWS provider."""
    from genesis.scheduler.install_test import run_install_test

    mock_provider = MagicMock(spec=AWSProvider)
    mock_provider.name = "aws"
    mock_provider.provision = AsyncMock(return_value=VMInstance(
        instance_id="i-mock",
        provider="aws",
        ip="54.0.0.1",
        ssh_key_path="/tmp/mock.pem",
        region="us-east-1",
        metadata={"key_name": "mock-key"},
    ))
    mock_provider.wait_for_ssh = AsyncMock(return_value=True)
    mock_provider.ssh_command = AsyncMock(side_effect=[
        "Cloning into...",        # git clone
        "Install complete!",      # install script
        "active",                 # systemctl check
        '{"status":"healthy"}',   # health check
    ])
    mock_provider.teardown = AsyncMock()

    with patch("genesis.scheduler.vm_provider.get_provider", return_value=mock_provider):
        result = await run_install_test({"provider": "aws"})

    assert result["success"] is True
    assert result["provider"] == "aws"
    mock_provider.teardown.assert_called_once()
