"""Install test runner — provisions a cloud VM, runs the public install
script, verifies it works, tears down the VM, and reports results.

This is the first user job type. It runs weekly (default: Sunday 2am UTC)
to ensure the public install script works for new users.

The runner:
1. Provisions a fresh cloud VM via the configured provider
2. Waits for SSH availability
3. Clones the public repo and runs scripts/install.sh --non-interactive
4. Captures output and classifies errors (transient vs structural)
5. Runs smoke tests (systemd services up, health endpoint responds)
6. Tears down the VM (always, even on failure)
7. Reports results via Telegram
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

# Default config for the install test job
DEFAULT_CONFIG = {
    "provider": "aws",
    "region": "us-east-1",
    "instance_type": "t2.micro",
    "image_id": "",  # Empty = auto-resolve latest Ubuntu 24.04 via SSM
    "repo_url": "https://github.com/WingedGuardian/GENesis-AGI.git",
    "install_command": "scripts/install.sh --non-interactive",
    "smoke_timeout_s": 120,
}

# Error patterns that indicate structural issues (not transient)
_STRUCTURAL_PATTERNS = [
    "ModuleNotFoundError",
    "No such file or directory",
    "command not found",
    "Permission denied",
    "SyntaxError",
    "ImportError",
    "pkg_resources.DistributionNotFound",
    "FileNotFoundError",
]

# Error patterns that indicate transient issues (retry-worthy)
_TRANSIENT_PATTERNS = [
    "Could not resolve host",
    "Connection timed out",
    "Temporary failure in name resolution",
    "Unable to fetch some archives",
    "Hash Sum mismatch",
    "Failed to fetch",
    "Connection reset by peer",
    "503 Service Temporarily Unavailable",
]


def classify_error(output: str) -> str:
    """Classify install output as 'structural', 'transient', or 'unknown'."""
    for pattern in _STRUCTURAL_PATTERNS:
        if pattern in output:
            return "structural"
    for pattern in _TRANSIENT_PATTERNS:
        if pattern in output:
            return "transient"
    return "unknown"


async def run_install_test(config: dict) -> dict:
    """Execute a full install test cycle.

    Args:
        config: Provider-specific config merged with DEFAULT_CONFIG.

    Returns:
        dict with keys: success, provider, duration_s, install_output,
        smoke_output, error_type, error_message
    """
    from genesis.scheduler.vm_provider import VMInstance, get_provider

    merged = {**DEFAULT_CONFIG, **config}
    provider_name = merged["provider"]
    start = datetime.now(UTC)
    instance: VMInstance | None = None
    result = {
        "success": False,
        "provider": provider_name,
        "duration_s": 0,
        "install_output": "",
        "smoke_output": "",
        "error_type": None,
        "error_message": None,
    }

    try:
        provider = get_provider(provider_name)

        # 1. Provision VM
        logger.info("Install test: provisioning %s VM in %s", provider_name, merged.get("region"))
        instance = await provider.provision(merged)
        logger.info("Install test: VM %s at %s", instance.instance_id, instance.ip)

        # 2. Wait for SSH
        ssh_ok = await provider.wait_for_ssh(instance)
        if not ssh_ok:
            result["error_type"] = "transient"
            result["error_message"] = "SSH not available after provisioning"
            return result

        # 3. Clone repo and run install
        repo_url = merged["repo_url"]
        install_cmd = merged["install_command"]
        clone_output = await provider.ssh_command(
            instance,
            f"git clone {repo_url} ~/GENesis-AGI 2>&1",
        )
        logger.info("Install test: clone complete (%d chars)", len(clone_output))

        install_output = await provider.ssh_command(
            instance,
            f"cd ~/GENesis-AGI && bash {install_cmd} 2>&1",
        )
        result["install_output"] = install_output[-5000:]  # Cap for storage
        logger.info("Install test: install script complete (%d chars)", len(install_output))

        # 4. Check for errors in install output
        # If the SSH command itself succeeded, the script exited 0
        # But we still check output for warning signs
        error_type = classify_error(install_output)
        if error_type == "structural":
            result["error_type"] = "structural"
            result["error_message"] = "Install script produced structural errors"
            return result

        # 5. Smoke tests
        smoke_output = ""
        try:
            # Check systemd services
            svc_output = await provider.ssh_command(
                instance,
                "systemctl --user is-active genesis-server 2>&1 || echo 'SERVICE_NOT_ACTIVE'",
            )
            smoke_output += f"Service check: {svc_output}\n"

            # Check health endpoint
            health_output = await provider.ssh_command(
                instance,
                "curl -sf http://localhost:5000/api/genesis/health 2>&1 || echo 'HEALTH_UNREACHABLE'",
            )
            smoke_output += f"Health check: {health_output[:500]}\n"

            result["smoke_output"] = smoke_output
            if "SERVICE_NOT_ACTIVE" in svc_output or "HEALTH_UNREACHABLE" in health_output:
                result["error_type"] = "structural"
                result["error_message"] = "Smoke tests failed — service not running or health unreachable"
                return result

        except Exception as exc:
            result["smoke_output"] = f"Smoke test error: {exc}"
            result["error_type"] = "unknown"
            result["error_message"] = f"Smoke test exception: {exc}"
            return result

        # Success
        result["success"] = True
        logger.info("Install test: PASSED")

    except NotImplementedError as exc:
        result["error_type"] = "config"
        result["error_message"] = str(exc)
        logger.warning("Install test: provider not implemented — %s", exc)

    except Exception as exc:
        result["error_type"] = classify_error(str(exc))
        result["error_message"] = str(exc)
        logger.exception("Install test failed")

    finally:
        # Always teardown
        if instance is not None:
            try:
                provider = get_provider(provider_name)
                await provider.teardown(instance)
                logger.info("Install test: VM %s terminated", instance.instance_id)
            except Exception:
                logger.warning("Install test: teardown failed", exc_info=True)

        result["duration_s"] = (datetime.now(UTC) - start).total_seconds()

    return result


async def notify_result(result: dict) -> None:
    """Send install test result via Telegram."""
    try:
        from genesis.runtime import GenesisRuntime

        rt = GenesisRuntime.instance()
        pipeline = getattr(rt, "_outreach_pipeline", None)
        if pipeline is None:
            logger.warning("No outreach pipeline — cannot notify install test result")
            return

        from genesis.outreach.types import OutreachCategory, OutreachRequest

        if result["success"]:
            message = (
                "Weekly install test PASSED\n"
                f"Provider: {result['provider']}\n"
                f"Duration: {result['duration_s']:.0f}s"
            )
            category = OutreachCategory.DIGEST
        else:
            message = (
                f"Weekly install test FAILED\n"
                f"Provider: {result['provider']}\n"
                f"Error type: {result.get('error_type', 'unknown')}\n"
                f"Error: {result.get('error_message', 'unknown')[:200]}\n"
                f"Duration: {result['duration_s']:.0f}s"
            )
            category = OutreachCategory.BLOCKER

        await pipeline.route_request(OutreachRequest(
            category=category,
            message=message,
            metadata={"source": "install_test", "result": json.dumps(result)[:1000]},
        ))
    except Exception:
        logger.warning("Failed to notify install test result", exc_info=True)
