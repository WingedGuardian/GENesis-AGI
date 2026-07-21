"""WS-2 B5 learned-knob substrate — the closed registry + ledgered file writes.

Design §5.3, SUBSTRATE ONLY: the knob file, the bounds validator, the ledgered
write path (`apply_knob_change`), and the startup applier that syncs DB-backed
knobs from file. The deterministic calibration TRIGGER (cell ok, n≥50,
2-window directional miss → ego proposal) is deliberately NOT built — no v1
calibration lane grades awareness/memory behavior, so it would be structurally
dormant; it lands with the lane that gives it evidence (tabled record).

The registry is a CLOSED set of three knob groups (§5.3):

1. ``awareness.signal_weights.<signal_name>`` — DB-backed
   (``signal_weights.current_weight``; baseline = the row's
   ``initial_weight``; the CRUD clamps to the row's min/max in SQL).
2. ``awareness.depth_thresholds.<Micro|Light|Deep|Strategic>`` — DB-backed
   (``depth_thresholds.threshold``; no initial column, so the baseline is
   captured from the live value at first apply and pinned in the file entry).
3. ``memory.activation_blend.<base|access|connectivity>`` — code-backed
   (the activation blend constants; ``memory/activation.py`` reads
   :func:`activation_blend` through its module-level seam).

File model (the immunity config split): the repo base
``config/learned_knobs.yaml`` ships as documentation + empty registry and is
NEVER machine-written (a mutated repo file would dirty the tree and fight
deploys). Learned entries live in the install-local overlay
``~/.genesis/config/learned_knobs.local.yaml`` — every write goes through
``cognitive_ledger.record_file_modification`` (actor ``ws2_effector``), so
pre-image capture, drift-guarded rollback, and the MCP rollback tool are all
inherited. Rollback = file rollback + re-sync (:func:`apply_learned_knobs_to_db`).

Bounds are validator-enforced per §5.3: step ≤5% of baseline per change,
cumulative ≤±20% of baseline. (The 14-day per-knob cooldown is trigger
policy, deferred with the trigger.)

Dependency rule: module-level imports are stdlib + yaml + genesis.env +
genesis._config_overlay only (the ws2_ledger_config rule) — DB CRUDs and the
cognitive ledger import lazily inside functions, so the memory/awareness hot
paths can import this module without dragging the learning stack.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from genesis._config_overlay import _resolve_overlay_path, merge_local_overlay
from genesis.env import repo_root

if TYPE_CHECKING:
    import aiosqlite

logger = logging.getLogger(__name__)

_CONFIG_NAME = "learned_knobs.yaml"

# Shipped activation-blend constants (memory/activation.py's coefficients —
# must sum to 1.0 at maximum; the file may retune within ±20% per component).
BLEND_DEFAULTS = {"base": 0.6, "access": 0.25, "connectivity": 0.15}

# The CLOSED registry — key must match exactly one pattern (§5.3).
_KNOB_PATTERNS = (
    re.compile(r"^awareness\.signal_weights\.(?P<name>[a-z0-9_]+)$"),
    re.compile(r"^awareness\.depth_thresholds\.(?P<name>Micro|Light|Deep|Strategic)$"),
    re.compile(r"^memory\.activation_blend\.(?P<name>base|access|connectivity)$"),
)

_STEP_LIMIT = 0.05  # ≤5% of baseline per change
_CUMULATIVE_LIMIT = 0.20  # ≤±20% of baseline from baseline
_EPS = 1e-9

_OVERLAY_HEADER = (
    "# Learned knob overrides — MACHINE-WRITTEN via the cognitive ledger\n"
    "# (actor ws2_effector). Hand-edits are allowed but the ledger pre-image\n"
    "# trail only covers writes made through apply_knob_change. Schema per\n"
    "# knob key: {baseline: <pinned>, current: <value>}. Bounds: each change\n"
    "# <=5% of baseline, cumulative <=+/-20% of baseline. See\n"
    "# config/learned_knobs.yaml for the closed key registry.\n"
)


def _base_path() -> Path:
    return repo_root() / "config" / _CONFIG_NAME


def _overlay_path() -> Path:
    return _resolve_overlay_path(_base_path())


def parse_knob_key(key: str) -> tuple[str, str] | None:
    """Return ``(group, name)`` for a registry key, or None if not in the
    closed set. group ∈ {signal_weights, depth_thresholds, activation_blend}."""
    for pat in _KNOB_PATTERNS:
        m = pat.match(key)
        if m:
            group = key.split(".")[1]
            return group, m.group("name")
    return None


def load_knobs() -> dict[str, dict[str, Any]]:
    """The merged knob entries — ``{key: {baseline, current}}``.

    Reads base ← overlay fresh per call (no cache — same live-read contract
    as ws2_ledger_config). Malformed layers degrade to empty.
    """
    base: dict[str, Any] = {}
    base_path = _base_path()
    try:
        loaded = yaml.safe_load(base_path.read_text()) or {}
        if isinstance(loaded, dict):
            base = loaded
    except Exception:
        logger.debug("learned_knobs base config unreadable at %s", base_path)
    try:
        base = merge_local_overlay(base, base_path)
    except Exception:
        logger.warning("learned_knobs overlay merge failed", exc_info=True)
    knobs = base.get("knobs")
    if not isinstance(knobs, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, entry in knobs.items():
        if parse_knob_key(str(key)) is None:
            logger.warning("learned_knobs: ignoring unregistered key %r", key)
            continue
        if not isinstance(entry, dict) or "current" not in entry:
            logger.warning("learned_knobs: ignoring malformed entry for %r", key)
            continue
        out[str(key)] = entry
    return out


def activation_blend() -> dict[str, float]:
    """The activation-blend coefficients with any learned overrides applied.

    Consumed by ``memory/activation.py``'s module-level seam. Falls back
    per-component to :data:`BLEND_DEFAULTS`; a value outside the ±20% bound
    of its shipped default is ignored (defense against hand-edited files —
    the blend shapes every memory activation score).
    """
    blend = dict(BLEND_DEFAULTS)
    try:
        for key, entry in load_knobs().items():
            parsed = parse_knob_key(key)
            if parsed is None or parsed[0] != "activation_blend":
                continue
            name = parsed[1]
            default = BLEND_DEFAULTS[name]
            try:
                value = float(entry["current"])
            except (TypeError, ValueError):
                continue
            if abs(value - default) <= _CUMULATIVE_LIMIT * default + _EPS:
                blend[name] = value
            else:
                logger.warning(
                    "learned_knobs: activation_blend.%s=%r outside ±20%% of default %.2f — ignored",
                    name,
                    value,
                    default,
                )
    except Exception:
        logger.debug("learned_knobs activation_blend read failed", exc_info=True)
    return blend


def validate_change(*, baseline: float, current: float, new_value: float) -> list[str]:
    """Bounds check per §5.3 — returns error strings (empty = valid)."""
    errors: list[str] = []
    if baseline <= 0:
        return [f"baseline must be positive, got {baseline!r}"]
    if abs(new_value - current) > _STEP_LIMIT * baseline + _EPS:
        errors.append(
            f"step {abs(new_value - current):.4f} exceeds 5% of baseline "
            f"({_STEP_LIMIT * baseline:.4f})"
        )
    if abs(new_value - baseline) > _CUMULATIVE_LIMIT * baseline + _EPS:
        errors.append(
            f"cumulative drift {abs(new_value - baseline):.4f} exceeds ±20% of "
            f"baseline ({_CUMULATIVE_LIMIT * baseline:.4f})"
        )
    return errors


async def _resolve_baseline_and_current(
    db: aiosqlite.Connection, key: str, group: str, name: str
) -> tuple[float, float]:
    """(baseline, current) for *key* — file entry first, then the source."""
    entry = load_knobs().get(key)
    if entry is not None and "baseline" in entry:
        return float(entry["baseline"]), float(entry["current"])

    if group == "signal_weights":
        from genesis.db.crud import signal_weights as sw_crud

        row = await sw_crud.get(db, name)
        if row is None:
            raise ValueError(f"unknown signal {name!r}")
        return float(row["initial_weight"]), float(row["current_weight"])
    if group == "depth_thresholds":
        from genesis.db.crud import depth_thresholds as dt_crud

        row = await dt_crud.get(db, name)
        if row is None:
            raise ValueError(f"unknown depth {name!r}")
        # No initial column — the live value at first apply IS the baseline
        # (pinned into the file entry from then on).
        return float(row["threshold"]), float(row["threshold"])
    return float(BLEND_DEFAULTS[name]), float(activation_blend()[name])


async def apply_knob_change(
    db: aiosqlite.Connection, key: str, new_value: float, *, reason: str = ""
) -> str | None:
    """Apply one validated knob change — THE write path for learned knobs.

    Validates the key against the closed registry and the §5.3 bounds, writes
    the overlay file through ``record_file_modification`` (actor
    ``ws2_effector`` — pre-image + rollback inherited), then syncs the value
    to its consumer (DB row via the clamped CRUDs, or the activation-blend
    module seam). Returns the cognitive-ledger mod id (None if only the
    ledger row failed — the file write itself raising propagates).

    Raises ``ValueError`` on an unregistered key, unknown signal/depth, or a
    bounds violation. This is called by the future calibration trigger and by
    deliberate operator action — never fire-and-forget.
    """
    parsed = parse_knob_key(key)
    if parsed is None:
        raise ValueError(f"knob {key!r} is not in the closed registry")
    group, name = parsed
    new_value = float(new_value)

    baseline, current = await _resolve_baseline_and_current(db, key, group, name)
    errors = validate_change(baseline=baseline, current=current, new_value=new_value)
    if errors:
        raise ValueError(f"knob {key!r}: " + "; ".join(errors))

    # Read the RAW overlay (not the merged view) so we only ever rewrite
    # install-local state, preserving unrelated hand-added entries.
    overlay_path = _overlay_path()
    overlay: dict[str, Any] = {}
    try:
        loaded = yaml.safe_load(overlay_path.read_text()) or {}
        if isinstance(loaded, dict):
            overlay = loaded
    except FileNotFoundError:
        pass
    except Exception:
        logger.warning("learned_knobs overlay unreadable — rewriting", exc_info=True)
    knobs = overlay.setdefault("knobs", {})
    if not isinstance(knobs, dict):
        knobs = overlay["knobs"] = {}
    knobs[key] = {"baseline": baseline, "current": new_value}

    body = _OVERLAY_HEADER + yaml.safe_dump(overlay, sort_keys=True)
    from genesis.learning.cognitive_ledger import record_file_modification

    mod_id = await record_file_modification(
        db,
        actor="ws2_effector",
        path=overlay_path,
        new_content=body,
        summary=(
            f"knob {key}: {current:.4f} → {new_value:.4f}" + (f" ({reason})" if reason else "")
        ),
        metadata={"knob": key, "baseline": baseline, "previous": current},
    )

    await _sync_knob(db, group, name, new_value)
    return mod_id


async def _sync_knob(db: aiosqlite.Connection, group: str, name: str, value: float) -> None:
    """Push one knob value to its consumer."""
    if group == "signal_weights":
        from genesis.db.crud import signal_weights as sw_crud

        await sw_crud.update_weight(db, name, new_weight=value)
    elif group == "depth_thresholds":
        from genesis.db.crud import depth_thresholds as dt_crud

        await dt_crud.update_threshold(db, name, new_threshold=value)
    else:  # activation_blend — poke the module-level seam
        try:
            from genesis.memory.activation import reload_blend

            reload_blend()
        except Exception:
            logger.debug("activation blend reload failed", exc_info=True)


async def apply_learned_knobs_to_db(db: aiosqlite.Connection) -> int:
    """Startup applier — sync every DB-backed file entry into its row.

    The file is the source of truth for learned values; a fresh DB (or a
    restore) re-converges here. Activation-blend entries need no DB sync
    (activation.py reads the file through its seam). Never raises; returns
    the number of knobs applied.
    """
    applied = 0
    try:
        for key, entry in load_knobs().items():
            parsed = parse_knob_key(key)
            if parsed is None or parsed[0] == "activation_blend":
                continue
            group, name = parsed
            try:
                baseline = float(entry.get("baseline", 0.0))
                value = float(entry["current"])
            except (TypeError, ValueError):
                logger.warning("learned_knobs: non-numeric entry for %r", key)
                continue
            errors = validate_change(baseline=baseline, current=value, new_value=value)
            # Startup re-sync only checks the CUMULATIVE bound (step vs
            # itself is 0); a corrupted/out-of-bounds entry is skipped loudly.
            if any("cumulative" in e or "baseline" in e for e in errors):
                logger.warning("learned_knobs: %r out of bounds — skipped: %s", key, errors)
                continue
            await _sync_knob(db, group, name, value)
            applied += 1
        if applied:
            logger.info("learned_knobs: applied %d knob(s) from file to DB", applied)
    except Exception:
        logger.warning("learned_knobs startup apply failed", exc_info=True)
    return applied
