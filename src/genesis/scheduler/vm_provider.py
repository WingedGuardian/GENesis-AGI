"""Provider-agnostic VM lifecycle interface for install testing.

Defines the VMProvider protocol and VMInstance dataclass used by
the install test runner to provision, manage, and tear down cloud
VMs. Provider implementations (AWS, Azure, GCP) are stubs until
their respective accounts are configured.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class VMInstance:
    """A provisioned cloud VM instance."""

    instance_id: str
    provider: str  # "aws", "azure", "gcp"
    ip: str
    ssh_user: str = "ubuntu"
    ssh_key_path: str = ""
    region: str = ""
    metadata: dict = field(default_factory=dict)


class VMProvider(ABC):
    """Abstract base for cloud VM providers."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Provider identifier (e.g., 'aws', 'azure', 'gcp')."""

    @abstractmethod
    async def provision(self, config: dict) -> VMInstance:
        """Provision a new VM from config.

        Config keys (provider-specific):
          - region: Cloud region
          - instance_type: VM size/type
          - image_id: AMI/image to boot
          - ssh_key_name: SSH key pair name
          - tags: dict of resource tags

        Returns a VMInstance with connection details.
        Raises RuntimeError if provisioning fails.
        """

    @abstractmethod
    async def teardown(self, instance: VMInstance) -> None:
        """Terminate the VM and clean up resources.

        Must be idempotent — calling on an already-terminated
        instance should not raise.
        """

    @abstractmethod
    async def ssh_command(self, instance: VMInstance, cmd: str) -> str:
        """Execute a command on the VM via SSH.

        Returns stdout. Raises RuntimeError on failure (non-zero exit
        or SSH connection error).
        """

    async def wait_for_ssh(
        self, instance: VMInstance, *, max_wait_s: int = 300, poll_s: int = 10,
    ) -> bool:
        """Poll until SSH is available. Returns True if reachable."""
        import asyncio

        elapsed = 0
        while elapsed < max_wait_s:
            try:
                result = await self.ssh_command(instance, "echo ready")
                if "ready" in result:
                    return True
            except Exception:
                pass
            await asyncio.sleep(poll_s)
            elapsed += poll_s

        logger.warning(
            "VM %s SSH not available after %ds", instance.instance_id, max_wait_s,
        )
        return False


# ─── Provider Implementations (Stubs) ────────────────────────────────────────


class AWSProvider(VMProvider):
    """AWS EC2 provider — stub until credentials are configured."""

    @property
    def name(self) -> str:
        return "aws"

    async def provision(self, config: dict) -> VMInstance:
        raise NotImplementedError(
            "AWS provider not yet implemented. "
            "Requires: boto3, IAM credentials, VPC/subnet/SG setup. "
            "Config expects: region, instance_type, image_id, ssh_key_name."
        )

    async def teardown(self, instance: VMInstance) -> None:
        raise NotImplementedError("AWS teardown not yet implemented.")

    async def ssh_command(self, instance: VMInstance, cmd: str) -> str:
        raise NotImplementedError("AWS SSH not yet implemented.")


class AzureProvider(VMProvider):
    """Azure VM provider — stub until credentials are configured."""

    @property
    def name(self) -> str:
        return "azure"

    async def provision(self, config: dict) -> VMInstance:
        raise NotImplementedError(
            "Azure provider not yet implemented. "
            "Requires: azure-mgmt-compute SDK, service principal credentials. "
            "Config expects: region, vm_size, image_reference, ssh_key_name."
        )

    async def teardown(self, instance: VMInstance) -> None:
        raise NotImplementedError("Azure teardown not yet implemented.")

    async def ssh_command(self, instance: VMInstance, cmd: str) -> str:
        raise NotImplementedError("Azure SSH not yet implemented.")


class GCPProvider(VMProvider):
    """GCP Compute Engine provider — stub until credentials are configured."""

    @property
    def name(self) -> str:
        return "gcp"

    async def provision(self, config: dict) -> VMInstance:
        raise NotImplementedError(
            "GCP provider not yet implemented. "
            "Requires: google-cloud-compute SDK, service account key. "
            "Config expects: zone, machine_type, image_family, ssh_key."
        )

    async def teardown(self, instance: VMInstance) -> None:
        raise NotImplementedError("GCP teardown not yet implemented.")

    async def ssh_command(self, instance: VMInstance, cmd: str) -> str:
        raise NotImplementedError("GCP SSH not yet implemented.")


# ─── Provider Registry ────────────────────────────────────────────────────────

_PROVIDERS: dict[str, type[VMProvider]] = {
    "aws": AWSProvider,
    "azure": AzureProvider,
    "gcp": GCPProvider,
}


def get_provider(name: str) -> VMProvider:
    """Get a provider instance by name. Raises KeyError if unknown."""
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise KeyError(
            f"Unknown VM provider '{name}'. Available: {', '.join(_PROVIDERS.keys())}"
        )
    return cls()
