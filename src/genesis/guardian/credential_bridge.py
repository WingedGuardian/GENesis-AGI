"""Telegram credential bridge — BOTH SIDES. Propagates credentials via shared filesystem.

Genesis (container) owns the secrets file. This module extracts only the
Telegram credentials and writes them to the shared Incus mount, where
Guardian (host) reads them. The full secrets file never leaves the container.

Container side: propagate_telegram_credentials() — called from awareness tick
Host side: load_telegram_credentials() — called from check.py dispatcher

Both sides see the same file via Incus shared mount with shift=true.
Container writes to ~/.genesis/shared/guardian/telegram_creds.env,
host reads from $STATE_DIR/shared/guardian/telegram_creds.env.
"""

from __future__ import annotations

import logging
import os
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "load_provisioning_credentials",
    "load_telegram_credentials",
    "propagate_guardian_credentials",
    "propagate_provisioning_credentials",
    "propagate_telegram_credentials",
]

# Container-side paths
_CONTAINER_SHARED_DIR = Path("~/.genesis/shared").expanduser()
_CONTAINER_SECRETS = Path("~/genesis/secrets.env").expanduser()

# Output filename (same on both sides of the mount)
_CREDS_FILENAME = "telegram_creds.env"
_CREDS_SUBDIR = "guardian"

# Provisioning (Proxmox) credential propagation — only the two API token
# strings cross the bridge; host/node/vmid are non-secret config, not here.
_PROVISIONING_FILENAME = "proxmox_creds.env"
_KEY_MAP_PROVISIONING = {
    "PROXMOX_AUDIT_TOKEN": "PROXMOX_AUDIT_TOKEN",
    "PROXMOX_PROVISION_TOKEN": "PROXMOX_PROVISION_TOKEN",
}

# Keys to extract from container secrets.env
# Maps source key name → output key name
_KEY_MAP = {
    "TELEGRAM_BOT_TOKEN": "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_FORUM_CHAT_ID": "TELEGRAM_CHAT_ID",  # Guardian uses CHAT_ID
    "TELEGRAM_CHAT_ID": "TELEGRAM_CHAT_ID",  # Also accept direct name
    "TELEGRAM_THREAD_ID": "TELEGRAM_THREAD_ID",
}


def propagate_telegram_credentials(
    shared_dir: Path | None = None,
    secrets_path: Path | None = None,
) -> Path | None:
    """Extract Telegram credentials from secrets.env and write to shared mount.

    Called from the container side (awareness loop tick). Writes only the
    Telegram keys Guardian needs — no other secrets are exposed.

    Returns the path written, or None if no bot token found.
    """
    src = secrets_path or _CONTAINER_SECRETS
    out_dir = (shared_dir or _CONTAINER_SHARED_DIR) / _CREDS_SUBDIR

    # Read source secrets
    source_secrets = _read_dotenv(src)
    if not source_secrets:
        logger.debug("No secrets file at %s — skipping credential propagation", src)
        return None

    # Extract and map Telegram keys
    creds: dict[str, str] = {}
    for src_key, dst_key in _KEY_MAP.items():
        value = source_secrets.get(src_key, "")
        if value and dst_key not in creds:  # First match wins (FORUM_CHAT_ID before CHAT_ID)
            creds[dst_key] = value

    if not creds.get("TELEGRAM_BOT_TOKEN"):
        logger.debug("No TELEGRAM_BOT_TOKEN in %s — skipping", src)
        return None

    out_path = _write_creds_atomic(out_dir, _CREDS_FILENAME, creds)
    logger.debug("Telegram credentials propagated to %s (%d keys)", out_path, len(creds))
    return out_path


def load_telegram_credentials(
    state_dir: str = "~/.local/state/genesis-guardian",
) -> dict[str, str]:
    """Read Telegram credentials from the shared mount (host side).

    Returns a dict with TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, etc.
    Returns empty dict if the file is missing or unreadable — the caller
    should fall back to other credential sources.
    """
    creds_path = Path(state_dir).expanduser() / "shared" / _CREDS_SUBDIR / _CREDS_FILENAME

    if not creds_path.exists():
        logger.debug("Telegram credentials not found at %s", creds_path)
        return {}

    try:
        return _read_dotenv(creds_path)
    except OSError as exc:
        logger.warning("Failed to read Telegram credentials: %s", exc)
        return {}


def propagate_provisioning_credentials(
    shared_dir: Path | None = None,
    secrets_path: Path | None = None,
) -> Path | None:
    """Extract Proxmox API tokens from secrets.env → shared mount (host reads).

    Only PROXMOX_AUDIT_TOKEN / PROXMOX_PROVISION_TOKEN are exposed. Requires at
    least the audit token (read-only capacity works with audit alone); the
    provision token is propagated when present. Returns the path written, or
    None if the audit token is absent.
    """
    src = secrets_path or _CONTAINER_SECRETS
    out_dir = (shared_dir or _CONTAINER_SHARED_DIR) / _CREDS_SUBDIR

    source_secrets = _read_dotenv(src)
    if not source_secrets:
        logger.debug("No secrets file at %s — skipping provisioning propagation", src)
        return None

    creds: dict[str, str] = {}
    for src_key, dst_key in _KEY_MAP_PROVISIONING.items():
        value = source_secrets.get(src_key, "")
        if value:
            creds[dst_key] = value

    if not creds.get("PROXMOX_AUDIT_TOKEN"):
        logger.debug("No PROXMOX_AUDIT_TOKEN in %s — skipping provisioning creds", src)
        return None

    out_path = _write_creds_atomic(out_dir, _PROVISIONING_FILENAME, creds)
    logger.debug("Provisioning credentials propagated to %s (%d keys)", out_path, len(creds))
    return out_path


def load_provisioning_credentials(
    state_dir: str = "~/.local/state/genesis-guardian",
) -> dict[str, str]:
    """Read Proxmox API tokens from the shared mount (host side).

    Returns {} when absent/unreadable — the caller falls back to legacy
    secrets or refuses to build the adapter.
    """
    creds_path = (
        Path(state_dir).expanduser() / "shared" / _CREDS_SUBDIR / _PROVISIONING_FILENAME
    )
    if not creds_path.exists():
        logger.debug("Provisioning credentials not found at %s", creds_path)
        return {}
    try:
        return _read_dotenv(creds_path)
    except OSError as exc:
        logger.warning("Failed to read provisioning credentials: %s", exc)
        return {}


def propagate_guardian_credentials(
    shared_dir: Path | None = None,
    secrets_path: Path | None = None,
) -> list[Path]:
    """Combined container-side bridge: telegram + provisioning, each guarded.

    Wired to the awareness-loop tick (called zero-arg). A failure in one leg
    never blocks the other, and never raises into the loop.
    """
    written: list[Path] = []
    for fn in (propagate_telegram_credentials, propagate_provisioning_credentials):
        try:
            path = fn(shared_dir=shared_dir, secrets_path=secrets_path)
        except Exception as exc:  # noqa: BLE001 — must never break the tick
            logger.warning("credential propagation (%s) failed: %s", fn.__name__, exc)
            continue
        if path:
            written.append(path)
    return written


def _write_creds_atomic(out_dir: Path, filename: str, creds: dict[str, str]) -> Path:
    """Atomic 0600 write of a creds dict; skip the write if unchanged."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / filename
    new_content = "".join(f"{k}={v}\n" for k, v in sorted(creds.items()))

    if out_path.exists():
        try:
            if out_path.read_text() == new_content:
                return out_path
        except OSError:
            pass  # File unreadable — rewrite it

    tmp_path = out_dir / f".{filename}.tmp"
    tmp_path.write_text(new_content)
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)  # 600
    os.replace(tmp_path, out_path)
    return out_path


def _read_dotenv(path: Path) -> dict[str, str]:
    """Parse a simple key=value file. Handles comments and optional quotes."""
    if not path.exists():
        return {}

    result: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            key = key.strip()
            # Handle 'export KEY=value' syntax
            if key.startswith("export "):
                key = key[7:].strip()
            value = value.strip().strip("'\"")
            result[key] = value
    return result
