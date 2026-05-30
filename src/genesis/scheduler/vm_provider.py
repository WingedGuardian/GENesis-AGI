"""Provider-agnostic VM lifecycle interface for install testing.

Defines the VMProvider protocol and VMInstance dataclass used by
the install test runner to provision, manage, and tear down cloud
VMs. AWS is fully implemented; Azure and GCP are stubs.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Where ephemeral SSH keys live during a test run
_KEY_DIR = Path(os.environ.get("GENESIS_TMP", str(Path.home() / "tmp")))


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


# ─── SSH Helper ──────────────────────────────────────────────────────────────


async def _run_ssh(
    ip: str, user: str, key_path: str, cmd: str, *, timeout_s: int = 600,
) -> str:
    """Run a command on a remote host via the system ssh client."""
    ssh_args = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ConnectTimeout=15",
        "-o", "ServerAliveInterval=30",
        "-i", key_path,
        f"{user}@{ip}",
        cmd,
    ]
    proc = await asyncio.create_subprocess_exec(
        *ssh_args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        raise RuntimeError(f"SSH command timed out after {timeout_s}s: {cmd[:80]}") from None

    out = stdout.decode(errors="replace")
    err = stderr.decode(errors="replace")
    if proc.returncode != 0:
        # Include both stdout and stderr in error for diagnosis
        combined = (out + "\n" + err).strip()
        raise RuntimeError(
            f"SSH command failed (rc={proc.returncode}): "
            f"{combined[-1000:]}"
        )
    return out


# ─── AWS Provider ────────────────────────────────────────────────────────────


# Ubuntu 24.04 LTS AMI SSM parameters (canonical's official)
_UBUNTU_SSM_PARAMS = {
    "x86_64": "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id",
    "arm64": "/aws/service/canonical/ubuntu/server/24.04/stable/current/arm64/hvm/ebs-gp3/ami-id",
}

# Instance types known to be ARM64 (Graviton)
_ARM64_PREFIXES = ("t4g", "m6g", "m7g", "c6g", "c7g", "r6g", "r7g", "a1")

# Tag used to find/reuse the Genesis security group
_SG_TAG = "genesis-install-test"


class AWSProvider(VMProvider):
    """AWS EC2 provider — provisions ephemeral VMs for install testing.

    Credentials come from the environment (AWS_ACCESS_KEY_ID /
    AWS_SECRET_ACCESS_KEY) or secrets.env. No pre-existing key pair
    or security group required — both are managed automatically.
    """

    @property
    def name(self) -> str:
        return "aws"

    def _get_ec2(self, region: str):
        """Get a boto3 EC2 resource for the given region."""
        import boto3
        return boto3.resource("ec2", region_name=region)

    def _get_ec2_client(self, region: str):
        """Get a boto3 EC2 client for the given region."""
        import boto3
        return boto3.client("ec2", region_name=region)

    def _get_ssm_client(self, region: str):
        """Get a boto3 SSM client for AMI lookup."""
        import boto3
        return boto3.client("ssm", region_name=region)

    async def _resolve_ami(self, region: str, instance_type: str) -> str:
        """Resolve the latest Ubuntu 24.04 AMI ID via SSM, matching architecture."""
        arch = "arm64" if instance_type.split(".")[0] in _ARM64_PREFIXES else "x86_64"
        ssm_param = _UBUNTU_SSM_PARAMS[arch]

        loop = asyncio.get_event_loop()
        ssm = self._get_ssm_client(region)
        resp = await loop.run_in_executor(
            None,
            lambda: ssm.get_parameter(Name=ssm_param),
        )
        ami_id = resp["Parameter"]["Value"]
        logger.info("Resolved Ubuntu 24.04 AMI (%s): %s in %s", arch, ami_id, region)
        return ami_id

    async def _ensure_security_group(self, region: str) -> str:
        """Find or create the genesis-install-test security group."""
        loop = asyncio.get_event_loop()
        client = self._get_ec2_client(region)

        # Check if it already exists
        try:
            resp = await loop.run_in_executor(
                None,
                lambda: client.describe_security_groups(
                    Filters=[{"Name": "group-name", "Values": [_SG_TAG]}],
                ),
            )
            if resp["SecurityGroups"]:
                sg_id = resp["SecurityGroups"][0]["GroupId"]
                logger.info("Reusing security group %s", sg_id)
                return sg_id
        except Exception:
            pass

        # Create it
        resp = await loop.run_in_executor(
            None,
            lambda: client.create_security_group(
                GroupName=_SG_TAG,
                Description="Ephemeral SG for Genesis install testing - SSH only",
            ),
        )
        sg_id = resp["GroupId"]

        # Allow SSH from anywhere (the VM is ephemeral and short-lived)
        await loop.run_in_executor(
            None,
            lambda: client.authorize_security_group_ingress(
                GroupId=sg_id,
                IpPermissions=[{
                    "IpProtocol": "tcp",
                    "FromPort": 22,
                    "ToPort": 22,
                    "IpRanges": [{"CidrIp": "0.0.0.0/0", "Description": "SSH for install test"}],
                }],
            ),
        )
        # Tag it
        await loop.run_in_executor(
            None,
            lambda: client.create_tags(
                Resources=[sg_id],
                Tags=[{"Key": "Name", "Value": _SG_TAG}],
            ),
        )
        logger.info("Created security group %s", sg_id)
        return sg_id

    async def _create_key_pair(self, region: str, run_id: str) -> tuple[str, str]:
        """Create an ephemeral EC2 key pair. Returns (key_name, key_file_path)."""
        loop = asyncio.get_event_loop()
        client = self._get_ec2_client(region)
        key_name = f"genesis-install-test-{run_id}"

        resp = await loop.run_in_executor(
            None,
            lambda: client.create_key_pair(
                KeyName=key_name, KeyType="ed25519",
            ),
        )
        key_material = resp["KeyMaterial"]

        # Write to a temp file
        _KEY_DIR.mkdir(parents=True, exist_ok=True)
        key_path = _KEY_DIR / f"{key_name}.pem"
        key_path.write_text(key_material)
        key_path.chmod(0o600)
        logger.info("Created ephemeral key pair %s → %s", key_name, key_path)
        return key_name, str(key_path)

    async def _delete_key_pair(self, region: str, key_name: str, key_path: str) -> None:
        """Delete the ephemeral key pair from AWS and local disk."""
        loop = asyncio.get_event_loop()
        client = self._get_ec2_client(region)
        try:
            await loop.run_in_executor(
                None,
                lambda: client.delete_key_pair(KeyName=key_name),
            )
            logger.info("Deleted EC2 key pair %s", key_name)
        except Exception:
            logger.warning("Failed to delete EC2 key pair %s", key_name, exc_info=True)

        with contextlib.suppress(Exception):
            Path(key_path).unlink(missing_ok=True)

    async def provision(self, config: dict) -> VMInstance:
        """Provision an EC2 instance for install testing."""
        import uuid

        region = config.get("region", "us-east-1")
        instance_type = config.get("instance_type", "t3.medium")
        run_id = uuid.uuid4().hex[:8]

        # Resolve AMI (architecture-aware)
        ami_id = config.get("image_id") or await self._resolve_ami(region, instance_type)

        # Ensure security group
        sg_id = await self._ensure_security_group(region)

        # Create ephemeral key pair
        key_name, key_path = await self._create_key_pair(region, run_id)

        # Launch instance
        loop = asyncio.get_event_loop()
        ec2 = self._get_ec2(region)
        try:
            instances = await loop.run_in_executor(
                None,
                lambda: ec2.create_instances(
                    ImageId=ami_id,
                    InstanceType=instance_type,
                    KeyName=key_name,
                    SecurityGroupIds=[sg_id],
                    MinCount=1,
                    MaxCount=1,
                    TagSpecifications=[{
                        "ResourceType": "instance",
                        "Tags": [
                            {"Key": "Name", "Value": f"genesis-install-test-{run_id}"},
                            {"Key": "genesis-role", "Value": "install-test"},
                        ],
                    }],
                    # 30GB root volume (install needs space for venv + qdrant)
                    BlockDeviceMappings=[{
                        "DeviceName": "/dev/sda1",
                        "Ebs": {
                            "VolumeSize": 30,
                            "VolumeType": "gp3",
                            "DeleteOnTermination": True,
                        },
                    }],
                ),
            )
        except Exception as exc:
            # Clean up key pair on launch failure
            await self._delete_key_pair(region, key_name, key_path)
            raise RuntimeError(f"EC2 launch failed: {exc}") from exc

        instance = instances[0]
        logger.info("Launched EC2 instance %s, waiting for running state", instance.id)

        # Wait for running
        await loop.run_in_executor(None, instance.wait_until_running)
        await loop.run_in_executor(None, instance.reload)

        public_ip = instance.public_ip_address
        if not public_ip:
            raise RuntimeError(
                f"Instance {instance.id} has no public IP. "
                "Check that the default VPC subnet assigns public IPs."
            )

        logger.info("EC2 instance %s running at %s", instance.id, public_ip)
        return VMInstance(
            instance_id=instance.id,
            provider="aws",
            ip=public_ip,
            ssh_user="ubuntu",
            ssh_key_path=key_path,
            region=region,
            metadata={
                "key_name": key_name,
                "sg_id": sg_id,
                "run_id": run_id,
            },
        )

    async def teardown(self, instance: VMInstance) -> None:
        """Terminate the EC2 instance and clean up the key pair."""
        loop = asyncio.get_event_loop()
        region = instance.region or "us-east-1"
        client = self._get_ec2_client(region)

        # Terminate
        try:
            await loop.run_in_executor(
                None,
                lambda: client.terminate_instances(InstanceIds=[instance.instance_id]),
            )
            logger.info("Terminated EC2 instance %s", instance.instance_id)
        except Exception:
            logger.warning(
                "Failed to terminate %s", instance.instance_id, exc_info=True,
            )

        # Clean up key pair
        key_name = instance.metadata.get("key_name", "")
        if key_name:
            await self._delete_key_pair(region, key_name, instance.ssh_key_path)

    async def ssh_command(self, instance: VMInstance, cmd: str) -> str:
        """Execute a command on the EC2 instance via SSH."""
        return await _run_ssh(
            instance.ip, instance.ssh_user, instance.ssh_key_path, cmd,
        )


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
