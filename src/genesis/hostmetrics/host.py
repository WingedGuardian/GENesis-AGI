"""Host leg: the host VM's memory, through the guardian's read-only ``ram-status``.

A container's cgroup limit can exceed what its host actually has free, so
memory is also judged against the host. The only host link is the guardian
gateway (SSH, read-only verb), configured in ``~/.genesis/guardian_remote.yaml``.

Never raises: any missing piece (no config, no PyYAML under system python, a
gateway error, an unrecognised reply) returns ``HostMemory`` with
``unavailable`` set to the reason, and the verdict says the host was not
checked. A healthy call takes about a second of SSH and is bounded at
``_CALL_TIMEOUT``; it runs once per process. Reasons are classified, never the
raw SSH text, which can carry the host's address and login.

Sync callers only: ``read_host_memory`` runs its own event loop.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_CONFIG = Path.home() / ".genesis" / "guardian_remote.yaml"
_DEFAULT_KEY = "~/.ssh/genesis_guardian_ed25519"

# guardian/memory_watch.py measure_host_mem_pct() detail: "<pct>% used (<used>M / <total>M)".
# A contract test feeds that function's output through this parser.
_DETAIL_RE = re.compile(r"\((\d+)M / (\d+)M\)")
_MIB = 1024 * 1024
# A hung gateway or sshd would otherwise hold a preflight for the gateway's own
# 70 s verb timeout. Healthy calls measured 0.9-1.6 s; 15 s is ten times that.
_CALL_TIMEOUT = 15.0

logger = logging.getLogger(__name__)


def _classify(error: str) -> str:
    """A safe one-word-ish reason for a gateway error; the raw text goes to debug."""
    logger.debug("guardian ram-status error: %s", error)
    low = error.lower()
    if low == "denied":
        return "gateway refused the verb (guardian predates ram-status?)"
    if "permission denied" in low:
        return "SSH authentication failed"
    if "timed out" in low or "timeout" in low:
        return "timed out"
    if "non-json" in low:
        return "gateway reply was not JSON"
    return "gateway or SSH error"


@dataclass(frozen=True)
class HostMemory:
    total: int | None = None  # bytes
    used: int | None = None  # bytes (total − MemAvailable)
    unavailable: str | None = None  # reason, when the host was not read


def parse_ram_status(payload: dict) -> HostMemory:
    """Host total/used from a ``ram-status`` reply."""
    if not payload.get("ok"):
        return HostMemory(
            unavailable=f"guardian ram-status: {_classify(str(payload.get('error', '')))}"
        )
    host = payload.get("host") or {}
    if host.get("used_pct") is None:
        return HostMemory(unavailable="host memory not measured by the guardian")
    match = _DETAIL_RE.search(str(host.get("detail", "")))
    if not match:
        return HostMemory(unavailable="unrecognised host detail in ram-status")
    used, total = (int(g) * _MIB for g in match.groups())
    return HostMemory(total=total, used=used)


def _default_remote(host_ip: str, host_user: str, key_path: str):
    from genesis.guardian.remote import GuardianRemote

    return GuardianRemote(host_ip=host_ip, host_user=host_user, key_path=key_path)


def read_host_memory(
    config_path: Path = _CONFIG,
    remote_factory: Callable[[str, str, str], object] = _default_remote,
) -> HostMemory:
    """One ``ram-status`` call, parsed. Unavailable (with the reason) on any failure."""
    if not config_path.exists():
        return HostMemory(unavailable="no host link configured (guardian_remote.yaml absent)")
    try:
        import yaml
    except ImportError:
        return HostMemory(unavailable="PyYAML not importable (run under the Genesis venv)")
    try:
        config = yaml.safe_load(config_path.read_text()) or {}
        host_ip, host_user = config.get("host_ip"), config.get("host_user")
        if not host_ip or not host_user:
            return HostMemory(unavailable="guardian_remote.yaml lacks host_ip/host_user")
        remote = remote_factory(host_ip, host_user, config.get("ssh_key") or _DEFAULT_KEY)
        payload = asyncio.run(asyncio.wait_for(remote.ram_status(), _CALL_TIMEOUT))
    except TimeoutError:
        return HostMemory(unavailable=f"host call timed out after {_CALL_TIMEOUT:g}s")
    except Exception as exc:  # noqa: BLE001 — the host leg degrades, never fails the verdict
        logger.debug("host leg call failed", exc_info=True)
        return HostMemory(unavailable=f"host call failed: {type(exc).__name__}")
    return parse_ram_status(payload if isinstance(payload, dict) else {})


@functools.cache
def host_memory_once() -> HostMemory:
    """``read_host_memory()`` cached for the life of the process."""
    return read_host_memory()
