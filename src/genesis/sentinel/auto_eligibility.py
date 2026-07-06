"""Reversibility classifier for Sentinel proposed actions — SHADOW mode.

Classifies each CC-proposed action as `auto_eligible` (reversible,
programmatic, matches a known-safe command shape) or `gated` (everything
else). This is observe-only groundwork: in `mode: shadow` (the default)
the classification is LOGGED to sentinel_log.jsonl and nothing about the
execution path changes — every action still requires human approval.

The accumulated `would_auto_run` / `would_gate` log lines calibrate the
allowlist against real incident data before any live flip. `mode: live`
(autonomous execution of auto_eligible actions + user notification) is a
separate, data-gated decision; the config value is reserved but NOT
implemented — the dispatcher treats it as shadow and warns.

Classification is deliberately pessimistic:
1. Self-fatal patterns (anything that would kill the Sentinel's own host
   process, per SENTINEL.md Hard Constraints) are checked FIRST and are
   never auto-eligible, regardless of tags.
2. The CC session's own `safe` + `reversible` tags must both be True.
3. The command must FULLY match an anchored allowlist shape derived from
   the SENTINEL.md Failure Inventory. Commands run via
   `create_subprocess_shell`, so an allowlisted prefix followed by a
   chained payload (`; rm -rf /`) must not match — hence full-string
   anchoring, not prefix matching.
Any doubt → `gated`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

AUTONOMY_MODE_SHADOW = "shadow"
AUTONOMY_MODE_LIVE = "live"

# Repo-root derivation assumes the src-layout editable install Genesis
# ships with; under a site-packages install the path misses and the mode
# silently defaults to shadow — the safe direction.
_CONFIG_PATH = Path(__file__).resolve().parents[3] / "config" / "sentinel.yaml"

# ── Self-fatal deny patterns (checked first, match anywhere) ─────────────
# The Sentinel runs inside the genesis-server process: restarting/stopping
# it, or broad process kills, take down the Sentinel itself mid-action.
# rm -rf class is excluded because recursive deletion is unbounded blast
# radius for an autonomous tier (and can kill the CC shell / working dir).
_SELF_FATAL_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        # ANY systemctl invocation naming genesis-server: restart/stop/kill
        # take down the Sentinel's own host process, verb-anchored patterns
        # are bypassable via flag interposition (--no-block etc.), and even
        # `start` is nonsensical from in here (if the server is down, so is
        # the Sentinel). (?s) so an embedded newline can't split the match.
        re.compile(r"(?s)\bsystemctl\b.*genesis-server"),
        "systemctl on genesis-server (the Sentinel's own host process)",
    ),
    (re.compile(r"\b(?:kill|pkill|killall)\b"), "process kill"),
    (
        # Short (-r/-R, possibly bundled) and long (--recursive) forms.
        re.compile(r"(?s)\brm\b(?=.*\s(?:-\w*[rR]|--recursive))"),
        "recursive rm",
    ),
)

# ── Allowlist: anchored full-command shapes from the Failure Inventory ───
# Whitespace note: \s matches newlines, but fullmatch + the tight unit
# charset (no metacharacters, no spaces) means an embedded payload can
# never ride along — keep the charset tight if you extend this.
#
# Only KNOWN-SAFE units, not any unit: a generic pattern would mark e.g.
# `genesis-tmp-watchgod` (which kills CC sessions when it acts) as
# auto-eligible. Extend from shadow data, not speculation.
_SAFE_UNITS = r"(?:genesis-watchdog|genesis-bridge|qdrant)(?:\.(?:service|timer))?"
_ALLOWLIST: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(rf"^systemctl\s+--user\s+(?:start|restart)\s+{_SAFE_UNITS}$"),
        "known-safe user-unit start/restart",
    ),
    (
        re.compile(r"^(?:sudo\s+)?systemctl\s+restart\s+qdrant(?:\.service)?$"),
        "qdrant restart",
    ),
    (
        # Non-zero value shape only: `--vacuum-size=0` would wipe all logs.
        re.compile(
            r"^(?:sudo\s+)?journalctl\s+--vacuum-(?:size=[1-9][0-9]*[KMG]"
            r"|time=[1-9][0-9]*(?:s|min|h|days?|d|weeks?|w|months?|y))$",
        ),
        "journal vacuum",
    ),
    (
        re.compile(r"^sync\s+&&\s+echo\s+1\s+>\s+/proc/sys/vm/drop_caches$"),
        "page-cache drop",
    ),
    # NOTE: `find /tmp ... -delete` (Failure Inventory) is deliberately NOT
    # allowlisted — file deletion is irreversible, which contradicts the
    # auto-eligibility rule's own semantics (architect review 2026-07-05).
    # It stays gated; shadow data will show how often that costs us.
)


@dataclass(frozen=True)
class ClassifiedAction:
    command: str
    decision: str  # "auto_eligible" | "gated"
    reason: str


def classify_action(action: dict[str, Any] | Any) -> ClassifiedAction:
    """Classify one proposed action. Fail-safe: anything unexpected → gated."""
    if not isinstance(action, dict):
        return ClassifiedAction("", "gated", "malformed action (not a dict)")

    command = action.get("command")
    if not isinstance(command, str) or not command.strip():
        return ClassifiedAction("", "gated", "malformed action (no command)")
    command = command.strip()

    for pattern, label in _SELF_FATAL_PATTERNS:
        if pattern.search(command):
            return ClassifiedAction(command, "gated", f"self-fatal: {label}")

    if action.get("safe") is not True:
        return ClassifiedAction(command, "gated", "not tagged safe=true by diagnosis")
    if action.get("reversible") is not True:
        return ClassifiedAction(
            command, "gated", "not tagged reversible=true by diagnosis",
        )

    for pattern, label in _ALLOWLIST:
        if pattern.fullmatch(command):
            return ClassifiedAction(command, "auto_eligible", f"allowlist: {label}")

    return ClassifiedAction(command, "gated", "no allowlist match (default-deny)")


def load_sentinel_autonomy_mode(path: Path | None = None) -> str:
    """Load `autonomy.mode` from config/sentinel.yaml.

    Defaults to shadow on any problem (missing file, bad YAML, unknown
    value) — the safe direction is always observe-only.
    """
    cfg_path = path or _CONFIG_PATH
    if not cfg_path.exists():
        return AUTONOMY_MODE_SHADOW

    try:
        import yaml

        from genesis._config_overlay import merge_local_overlay

        raw = yaml.safe_load(cfg_path.read_text()) or {}
        raw = merge_local_overlay(raw, cfg_path)
    except Exception:
        logger.warning(
            "Failed to load sentinel config from %s; defaulting to shadow",
            cfg_path,
            exc_info=True,
        )
        return AUTONOMY_MODE_SHADOW

    if not isinstance(raw, dict):
        logger.warning(
            "Sentinel config at %s is not a mapping; defaulting to shadow", cfg_path,
        )
        return AUTONOMY_MODE_SHADOW

    autonomy = raw.get("autonomy")
    mode = autonomy.get("mode") if isinstance(autonomy, dict) else None
    if mode in (AUTONOMY_MODE_SHADOW, AUTONOMY_MODE_LIVE):
        return mode

    if mode is not None:
        logger.warning(
            "Unknown sentinel autonomy mode %r in %s; defaulting to shadow",
            mode,
            cfg_path,
        )
    return AUTONOMY_MODE_SHADOW
