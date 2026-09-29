"""Config loader for inbox monitor — YAML → InboxConfig."""

from __future__ import annotations

import dataclasses
import logging
import os
from pathlib import Path

import yaml

from genesis.inbox.types import InboxConfig

logger = logging.getLogger(__name__)


def load_inbox_config(path: str | Path) -> InboxConfig:
    """Load inbox config from a YAML file path.

    Merges ``config/inbox_monitor.local.yaml`` overlay (written by the
    dashboard settings panel) on top of the base file when present.
    """
    from genesis._config_overlay import merge_local_overlay

    base_path = Path(path)
    raw = yaml.safe_load(base_path.read_text()) or {}
    raw = merge_local_overlay(raw, base_path)
    return _parse(raw)


def load_inbox_config_from_string(text: str) -> InboxConfig:
    """Load inbox config from a YAML string."""
    raw = yaml.safe_load(text)
    return _parse(raw)


def _parse(raw: dict) -> InboxConfig:
    """Parse raw YAML dict into a validated InboxConfig."""
    if not isinstance(raw, dict):
        msg = "Config must be a YAML mapping"
        raise ValueError(msg)

    section = raw.get("inbox_monitor")
    if section is None:
        msg = "Config must contain 'inbox_monitor' section"
        raise ValueError(msg)

    if "watch_path" not in section:
        msg = "inbox_monitor.watch_path is required"
        raise KeyError(msg)

    # ONE source of defaults: the InboxConfig dataclass (#1953). Every key here
    # used to repeat its default as a separate literal, and a change to one copy
    # took effect only on installs where the YAML key was absent — so the drift
    # was invisible on the machine that made it (timeout_s once read 3600, 600
    # and 900 in three places).
    defaults = {f.name: f.default for f in dataclasses.fields(InboxConfig)}

    def _positive_int(key: str, *, minimum: int = 1) -> int:
        """Read an int that must be >= 1; degrade to the default LOUDLY.

        A bare int() accepted 0 and negatives from a hand-edited YAML or an
        overlay (the MCP validator never sees those paths): items_per_eval=0
        divides a drop into nothing, max_retries=0 parks on the first miss.
        int() was also too forgiving about TYPE: YAML ``true`` became 1, 2.7
        was truncated to 2, and ``.inf`` raised OverflowError out of the loader.
        A bool or a non-integral float is not an integer; an integral float
        (4.0) is.
        """
        raw_val = section.get(key, defaults[key])
        if isinstance(raw_val, bool) or (
            isinstance(raw_val, float) and not raw_val.is_integer()
        ):
            val = minimum - 1
        else:
            try:
                val = int(raw_val)
            except (TypeError, ValueError, OverflowError):
                val = minimum - 1
        if val < minimum:
            logger.warning(
                "inbox_monitor.%s=%r is not an integer >= %d — using default %r",
                key, raw_val, minimum, defaults[key],
            )
            return defaults[key]
        return val

    # The monitor reads this as `!= "enforce"`, so an unrecognised value
    # degrades to shadow — the safe direction, but it must not be SILENT: an
    # operator touching this lever has decided to act, and a typo would leave
    # the gate observing while they believed it was live. The MCP settings
    # validator rejects a bad value on that path; this covers a hand-edited
    # YAML or a local overlay, which the validator never sees.
    coverage_mode = str(section.get("url_coverage_mode", defaults["url_coverage_mode"]))
    if coverage_mode not in {"shadow", "enforce"}:
        logger.warning(
            "inbox_monitor.url_coverage_mode=%r is not 'shadow' or 'enforce' — "
            "running in shadow (the gate will observe and change nothing)",
            coverage_mode,
        )
        coverage_mode = "shadow"

    return InboxConfig(
        watch_path=Path(
            os.environ.get("GENESIS_INBOX_PATH", section["watch_path"]),
        ).expanduser(),
        response_dir=section.get("response_dir", defaults["response_dir"]),
        check_interval_seconds=_positive_int("check_interval_seconds"),
        batch_size=_positive_int("batch_size"),
        items_per_eval=_positive_int("items_per_eval"),
        enabled=bool(section.get("enabled", defaults["enabled"])),
        model=str(section.get("model", defaults["model"])),
        effort=str(section.get("effort", defaults["effort"])),
        timeout_s=_positive_int("timeout_s"),
        max_retries=_positive_int("max_retries"),
        url_coverage_mode=coverage_mode,
        recursive=bool(section.get("recursive", defaults["recursive"])),
        # 0 is a legitimate "no cooldown", so this one floors at zero.
        evaluation_cooldown_seconds=_positive_int(
            "evaluation_cooldown_seconds", minimum=0,
        ),
    )
