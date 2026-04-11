"""Phase 6 contribution — local install identity.

Provides a stable, locally-generated UUID per Genesis install. Used
for PR attribution when the user has NOT opted into real git identity
via `--identify`. The pseudonym form is
`contributor-<install-id-8>@genesis.local`.

No telemetry: the install.json file is local-only and never
transmitted except as a hash prefix in PR body metadata.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

from .findings import InstallInfo

logger = logging.getLogger(__name__)
_INSTALL_FILE = Path.home() / ".genesis" / "install.json"


def _install_path() -> Path:
    """Resolve the install.json path, honoring GENESIS_HOME env override."""
    override = os.environ.get("GENESIS_HOME")
    if override:
        return Path(override) / "install.json"
    return _INSTALL_FILE


def load_install_info() -> InstallInfo:
    """Load (or create on first access) the local install identity.

    Self-heals a corrupt install.json by regenerating it — but logs
    a WARNING so the user can tell when their stable pseudonym has
    rotated. Narrow exception catching: only OSError, JSON errors,
    missing required keys, and invalid-UUID strings trigger a reset.
    Anything else (e.g. a TypeError in InstallInfo construction)
    escapes so the bug is visible.
    """
    path = _install_path()
    if path.exists():
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
            install_id = data["install_id"]
            created_at = data["created_at"]
            fingerprint_file = data.get("fingerprint_file")
            # Basic validity check — install_id must parse as UUID
            uuid.UUID(install_id)
            return InstallInfo(
                install_id=install_id,
                created_at=created_at,
                fingerprint_file=fingerprint_file,
            )
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as e:
            logger.warning(
                "install.json at %s is unreadable (%s); regenerating. "
                "NOTE: your contributor pseudonym will rotate.",
                path, e, exc_info=True,
            )

    info = InstallInfo(
        install_id=str(uuid.uuid4()),
        created_at=datetime.now(UTC).isoformat(),
        fingerprint_file=None,
    )
    _write(info, path)
    return info


def get_install_id() -> str:
    """Convenience: return just the install UUID string."""
    return load_install_info().install_id


def pseudonym_email(install_id: str) -> str:
    """Return the pseudonymous contributor email for an install id.

    Only the first 8 chars of the UUID are used — enough to
    disambiguate on the server side, short enough for humans to read
    in PR metadata. The `genesis.local` TLD is non-routable per
    RFC 2606 (`.local` is reserved for mDNS/local use).
    """
    short = install_id.split("-")[0] if install_id else "unknown"
    return f"contributor-{short}@genesis.local"


def _write(info: InstallInfo, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(info.to_dict(), indent=2), encoding="utf-8")
    tmp.replace(path)  # atomic on POSIX
