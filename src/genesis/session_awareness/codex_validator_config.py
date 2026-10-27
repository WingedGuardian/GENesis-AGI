"""Calibration lock: only an exact local false boolean enables ledger writes."""

import logging

import yaml

from genesis._config_overlay import merge_local_overlay
from genesis.env import repo_root

logger = logging.getLogger(__name__)


def _base_path():
    return repo_root() / "config/codex_validator.yaml"


def valid_gate(value):
    return (
        isinstance(value, dict)
        and set(value) == {"dry_run_only"}
        and type(value["dry_run_only"]) is bool
    )


def dry_run_only() -> bool:
    """Reload every call; bad base or overlay can only force preview."""
    try:
        path = _base_path()
        base = yaml.safe_load(path.read_text())
        if not valid_gate(base) or base["dry_run_only"] is not True:
            return True
        merged = merge_local_overlay(base, path, strict=True)
        return not valid_gate(merged) or merged["dry_run_only"] is not False
    except Exception as exc:
        logger.warning("Validator config invalid; forcing preview (%s)", type(exc).__name__)
        return True
