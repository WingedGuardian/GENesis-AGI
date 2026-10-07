"""Opt-in transcript analytics settings, independent of analytics dependencies."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from genesis.env import genesis_home


@dataclass(frozen=True)
class Config:
    enabled: bool = False
    projects_dir: Path = field(default_factory=lambda: Path.home() / ".claude/projects")
    data_dir: Path = field(default_factory=lambda: genesis_home() / "analytics/transcripts")
    ram_bytes: int | None = None
    ram_pct: float = 25
    cpu_pct: float = 25
    evidence_records: int = 5
    evidence_bytes: int = 65536


def load(base_path: Path | None = None, *, ignore_kill: bool = False) -> Config:
    """Read base and strict private overlay; malformed settings never opt in."""
    if not ignore_kill and os.environ.get("GENESIS_TRANSCRIPT_ANALYTICS_DISABLED") == "1":
        return Config()
    import yaml

    from genesis._config_overlay import merge_local_overlay
    from genesis.env import repo_root

    path = base_path or repo_root() / "config/transcript_analytics.yaml"
    base = yaml.safe_load(path.read_text()) if path.exists() else {}
    if not isinstance(base, dict):
        raise ValueError("transcript analytics config must be a mapping")
    values = merge_local_overlay(base, path, strict=True)
    if type(values.get("enabled", False)) is not bool:
        raise ValueError("enabled must be a boolean")
    if values.get("scope", "all") != "all":
        raise ValueError("transcript analytics v1 requires scope: all")
    kwargs = {key: value for key, value in values.items() if key in Config.__dataclass_fields__}
    for key in ("projects_dir", "data_dir"):
        if key in kwargs:
            kwargs[key] = Path(kwargs[key]).expanduser()
            if not kwargs[key].is_absolute():
                raise ValueError(f"{key} must be absolute")
    for key in ("ram_pct", "cpu_pct"):
        value = kwargs.get(key, 25)
        if type(value) not in (int, float) or not 0 < value <= 100:
            raise ValueError(f"{key} must be in (0, 100]")
    for key in ("ram_bytes", "evidence_records", "evidence_bytes"):
        if key not in kwargs or (key == "ram_bytes" and kwargs[key] is None):
            continue
        value = kwargs[key]
        if type(value) is not int or value <= 0:
            raise ValueError(f"{key} must be a positive integer")
    return Config(**kwargs)


if __name__ == "__main__":
    # Provisioning checks fail closed, including corrupt overlays.
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--data-dir", action="store_true")
    mode.add_argument("--configured-data-dir", action="store_true")
    args = parser.parse_args()
    try:
        cfg = load(ignore_kill=args.configured_data_dir)
        if args.configured_data_dir or (args.data_dir and cfg.enabled):
            print(cfg.data_dir)
        raise SystemExit(0 if cfg.enabled or args.configured_data_dir else 1)
    except Exception as exc:
        parser.exit(2, f"transcript analytics configuration unavailable: {exc}\n")
