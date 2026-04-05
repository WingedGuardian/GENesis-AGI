"""Config-backed policy for autonomous Claude Code fallback handling."""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)

_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "autonomous_cli_policy.yaml"
_CONTAINER_SHARED_DIR = Path("~/.genesis/shared").expanduser()
_EXPORT_SUBDIR = "guardian"
_EXPORT_FILENAME = "autonomous_cli_policy.json"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class AutonomousCliPolicy:
    autonomous_cli_fallback_enabled: bool = True
    manual_approval_required: bool = True
    reask_interval_hours: int = 24
    approval_channel: str = "telegram"
    shared_export_enabled: bool = True
    source: str = "defaults"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_autonomous_cli_policy(path: Path | None = None) -> AutonomousCliPolicy:
    """Load policy from YAML with env fallback defaults."""
    cfg_path = path or _CONFIG_PATH
    defaults = AutonomousCliPolicy(
        autonomous_cli_fallback_enabled=_env_bool(
            "GENESIS_AUTONOMOUS_CLI_FALLBACK_ENABLED", True,
        ),
        manual_approval_required=_env_bool(
            "GENESIS_AUTONOMOUS_CLI_APPROVAL_ENABLED", True,
        ),
        reask_interval_hours=max(
            1, _env_int("GENESIS_AUTONOMOUS_CLI_REASK_INTERVAL_HOURS", 24),
        ),
        approval_channel=(
            os.environ.get("GENESIS_AUTONOMOUS_APPROVAL_CHANNEL", "telegram")
            .strip()
            .lower()
            or "telegram"
        ),
        shared_export_enabled=True,
        source="env_defaults",
    )

    if not cfg_path.exists():
        return defaults

    try:
        raw = yaml.safe_load(cfg_path.read_text()) or {}
    except Exception:
        logger.warning(
            "Failed to load autonomous CLI policy from %s; using env defaults",
            cfg_path,
            exc_info=True,
        )
        return defaults

    if not isinstance(raw, dict):
        logger.warning(
            "Autonomous CLI policy at %s is not a mapping; using env defaults",
            cfg_path,
        )
        return defaults

    return AutonomousCliPolicy(
        autonomous_cli_fallback_enabled=bool(
            raw.get(
                "autonomous_cli_fallback_enabled",
                defaults.autonomous_cli_fallback_enabled,
            ),
        ),
        manual_approval_required=bool(
            raw.get("manual_approval_required", defaults.manual_approval_required),
        ),
        reask_interval_hours=max(
            1,
            int(raw.get("reask_interval_hours", defaults.reask_interval_hours) or 24),
        ),
        approval_channel=str(
            raw.get("approval_channel", defaults.approval_channel) or "telegram",
        ).strip().lower() or "telegram",
        shared_export_enabled=bool(
            raw.get("shared_export_enabled", defaults.shared_export_enabled),
        ),
        source=f"config:{cfg_path.name}",
    )


class AutonomousCliPolicyExporter:
    """Export effective Genesis-side policy to the Guardian shared mount."""

    def __init__(self, *, policy_loader=load_autonomous_cli_policy) -> None:
        self._policy_loader = policy_loader
        self._last_export_at: str | None = None
        self._last_export_path: str | None = None
        self._last_error: str | None = None

    def export(
        self,
        shared_dir: Path | None = None,
    ) -> Path | None:
        policy = self._policy_loader()
        if not policy.shared_export_enabled:
            self._last_error = None
            return None

        out_dir = (shared_dir or _CONTAINER_SHARED_DIR) / _EXPORT_SUBDIR
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / _EXPORT_FILENAME
        payload = {
            "autonomous_cli_fallback_enabled": policy.autonomous_cli_fallback_enabled,
            "manual_approval_required": policy.manual_approval_required,
            "reask_interval_hours": policy.reask_interval_hours,
            "approval_channel": policy.approval_channel,
            "exported_at": datetime.now(UTC).isoformat(),
            "source": policy.source,
        }
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                dir=str(out_dir),
                prefix=".autonomous_cli_policy.",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                json.dump(payload, tmp, sort_keys=True)
                tmp.write("\n")
                tmp_path = Path(tmp.name)
            os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
            os.replace(tmp_path, out_path)
        except Exception as exc:
            self._last_error = str(exc)
            logger.error("Failed to export autonomous CLI policy", exc_info=True)
            return None

        self._last_export_at = payload["exported_at"]
        self._last_export_path = str(out_path)
        self._last_error = None
        return out_path

    def status(self) -> dict[str, Any]:
        policy = self._policy_loader()
        return {
            "effective_policy": policy.as_dict(),
            "last_export_at": self._last_export_at,
            "last_export_path": self._last_export_path,
            "last_export_error": self._last_error,
        }
