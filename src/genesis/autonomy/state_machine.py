"""AutonomyManager — loads autonomy state, enforces ceilings, drives level changes."""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite
import yaml

from genesis.autonomy.types import (
    CONTEXT_CEILING_MAP,
    AutonomyCategory,
    AutonomyLevel,
    AutonomyState,
    ContextCeiling,
)
from genesis.db.crud import autonomy as crud
from genesis.observability.types import Severity, Subsystem

logger = logging.getLogger(__name__)

_DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent / "config" / "autonomy.yaml"
)

# Mapping from AutonomyCategory to ContextCeiling for ceiling lookups.
_CATEGORY_TO_CEILING: dict[AutonomyCategory, ContextCeiling] = {
    AutonomyCategory.DIRECT_SESSION: ContextCeiling.DIRECT_SESSION,
    AutonomyCategory.BACKGROUND_COGNITIVE: ContextCeiling.BACKGROUND_COGNITIVE,
    AutonomyCategory.SUB_AGENT: ContextCeiling.SUB_AGENT,
    AutonomyCategory.OUTREACH: ContextCeiling.OUTREACH,
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _row_to_state(row: dict) -> AutonomyState:
    """Convert a CRUD dict row into an AutonomyState dataclass."""
    return AutonomyState(
        id=row["id"],
        category=AutonomyCategory(row["category"]),
        current_level=AutonomyLevel(row["current_level"]),
        earned_level=AutonomyLevel(row["earned_level"]),
        consecutive_corrections=row.get("consecutive_corrections", 0),
        total_successes=row.get("total_successes", 0),
        total_corrections=row.get("total_corrections", 0),
    )


class AutonomyManager:
    """Manages autonomy levels per category — DB-backed, ceiling-enforced.

    Loads config from autonomy.yaml for default levels.  All mutations
    persist through the CRUD layer and, when relevant, emit events via
    the event bus so the observability stack can react.
    """

    def __init__(
        self,
        *,
        db: aiosqlite.Connection,
        event_bus: object | None = None,
        config_path: Path | None = None,
    ) -> None:
        self._db = db
        self._event_bus = event_bus
        self._config = self._load_config(config_path or _DEFAULT_CONFIG_PATH)

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    @staticmethod
    def _load_config(path: Path) -> dict:
        """Load YAML config with graceful fallback to empty defaults."""
        if not path.exists():
            logger.warning(
                "Autonomy config not found at %s — using built-in defaults", path
            )
            return {}
        try:
            data = yaml.safe_load(path.read_text())
            return data if isinstance(data, dict) else {}
        except (yaml.YAMLError, OSError):
            logger.error(
                "Failed to parse autonomy config at %s — using built-in defaults",
                path,
                exc_info=True,
            )
            return {}

    def _default_level(self, category: str) -> int:
        """Return the configured default level for a category, or 1."""
        defaults = self._config.get("defaults", {})
        return int(defaults.get(category, 1))

    # ------------------------------------------------------------------
    # State loading
    # ------------------------------------------------------------------

    async def load_or_create_defaults(self) -> dict[str, AutonomyState]:
        """Load all category states from DB, creating missing ones with defaults.

        Returns a dict keyed by category name.
        """
        existing = await crud.list_all(self._db)
        by_category: dict[str, AutonomyState] = {
            row["category"]: _row_to_state(row) for row in existing
        }

        for cat in AutonomyCategory:
            if cat.value not in by_category:
                level = self._default_level(cat.value)
                row_id = str(uuid.uuid4())
                await crud.upsert(
                    self._db,
                    id=row_id,
                    category=cat.value,
                    current_level=level,
                    earned_level=level,
                    updated_at=_now_iso(),
                )
                by_category[cat.value] = AutonomyState(
                    id=row_id,
                    category=cat,
                    current_level=AutonomyLevel(level),
                    earned_level=AutonomyLevel(level),
                )
                logger.info(
                    "Created default autonomy state for %s at L%d", cat.value, level
                )

        return by_category

    async def get_state(self, category: str) -> AutonomyState | None:
        """Get the current autonomy state for a single category."""
        row = await crud.get_by_category(self._db, category)
        if row is None:
            return None
        return _row_to_state(row)

    # ------------------------------------------------------------------
    # Level queries
    # ------------------------------------------------------------------

    async def effective_level(self, category: str) -> int:
        """Return min(current_level, context_ceiling) for *category*.

        Returns 0 if the category doesn't exist in DB.
        """
        state = await self.get_state(category)
        if state is None:
            return 0

        try:
            ceiling_key = _CATEGORY_TO_CEILING[AutonomyCategory(category)]
        except (ValueError, KeyError):
            # Unknown category — no ceiling, just use current level.
            return int(state.current_level)

        ceiling = CONTEXT_CEILING_MAP.get(ceiling_key, 7)
        return min(int(state.current_level), ceiling)

    def check_ceiling(self, category: str, required_level: int) -> bool:
        """Synchronous check — True if the category's ceiling >= *required_level*.

        This uses the static CONTEXT_CEILING_MAP (no DB hit).  For a
        full check that includes the DB-stored current level, use
        :meth:`effective_level` instead.
        """
        try:
            ceiling_key = _CATEGORY_TO_CEILING[AutonomyCategory(category)]
        except (ValueError, KeyError):
            return False
        ceiling = CONTEXT_CEILING_MAP.get(ceiling_key, 0)
        return ceiling >= required_level

    # ------------------------------------------------------------------
    # Level mutations
    # ------------------------------------------------------------------

    async def set_level(
        self, category: str, level: int, *, reason: str = ""
    ) -> bool:
        """Set the current autonomy level for *category*.

        Validates 1-4 range.  Updates both current_level and earned_level
        (earned only ratchets up).  Returns True on success.
        """
        if level < 1 or level > 4:
            logger.warning("Rejected set_level(%s, %d) — out of 1-4 range", category, level)
            return False

        state = await self.get_state(category)
        if state is None:
            logger.error("Cannot set level — category %s not found in DB", category)
            return False

        new_earned = max(int(state.earned_level), level)
        await crud.upsert(
            self._db,
            id=state.id,
            category=category,
            current_level=level,
            earned_level=new_earned,
            updated_at=_now_iso(),
        )
        logger.info(
            "Autonomy level set: %s → L%d (earned L%d)%s",
            category,
            level,
            new_earned,
            f" reason={reason}" if reason else "",
        )
        return True

    async def restore_earned_level(self, category: str) -> bool:
        """Restore current_level to earned_level for *category*."""
        state = await self.get_state(category)
        if state is None:
            logger.error("Cannot restore — category %s not found in DB", category)
            return False

        await crud.upsert(
            self._db,
            id=state.id,
            category=category,
            current_level=int(state.earned_level),
            earned_level=int(state.earned_level),
            updated_at=_now_iso(),
        )
        logger.info(
            "Restored autonomy level for %s: L%d → L%d (earned)",
            category,
            int(state.current_level),
            int(state.earned_level),
        )
        return True

    async def promote(
        self, category: str, to_level: int, *, reason: str = ""
    ) -> bool:
        """Explicit promotion — only called on user approval.

        Returns True if promotion succeeded, False if category not found
        or to_level is not actually a promotion.
        """
        state = await self.get_state(category)
        if state is None:
            logger.error("Cannot promote — category %s not found in DB", category)
            return False

        level_before = int(state.current_level)
        success = await crud.promote(
            self._db, state.id, to_level=to_level, updated_at=_now_iso()
        )
        if success:
            self._emit_promotion_event(category, level_before, to_level)
            logger.info(
                "Autonomy promoted: %s L%d → L%d%s",
                category, level_before, to_level,
                f" reason={reason}" if reason else "",
            )
        return success

    async def force_regress(
        self, category: str, to_level: int = 1, *, reason: str = "user_revoked"
    ) -> bool:
        """Hard reset — resets BOTH current and earned level.

        Used for user revocation. Unlike Bayesian regression (which only
        lowers current_level), this resets earned_level too — a full reset.
        """
        state = await self.get_state(category)
        if state is None:
            logger.error("Cannot force_regress — category %s not found in DB", category)
            return False

        level_before = int(state.current_level)
        success = await crud.force_regress(
            self._db, state.id, to_level=to_level, reason=reason, updated_at=_now_iso()
        )
        if success and level_before != to_level:
            self._emit_regression_event(category, level_before, to_level, _now_iso())
            logger.warning(
                "Autonomy force-regressed: %s L%d → L%d (%s)",
                category, level_before, to_level, reason,
            )
        return success

    # ------------------------------------------------------------------
    # Correction / success tracking
    # ------------------------------------------------------------------

    async def record_correction(
        self, category: str, *, corrected_at: str
    ) -> tuple[bool, bool]:
        """Record a correction for *category*.

        Returns ``(success, regressed)``.  If a regression occurred, emits
        an ``autonomy.regression`` event via the event bus.
        """
        state = await self.get_state(category)
        if state is None:
            logger.error("Cannot record correction — category %s not found", category)
            return False, False

        level_before = int(state.current_level)
        success = await crud.record_correction(
            self._db, state.id, corrected_at=corrected_at, updated_at=_now_iso()
        )
        if not success:
            return False, False

        # Re-read to detect regression.
        updated = await self.get_state(category)
        level_after = int(updated.current_level) if updated else level_before
        regressed = level_after < level_before

        if regressed:
            logger.warning(
                "Autonomy regression for %s: L%d → L%d",
                category,
                level_before,
                level_after,
            )
            self._emit_regression_event(category, level_before, level_after, corrected_at)

        return True, regressed

    async def record_success(self, category: str) -> tuple[bool, bool]:
        """Record a successful autonomous action for *category*.

        Returns ``(success, promoted)``.  Promotion no longer happens
        automatically — it requires explicit user approval via promote().
        The second tuple element is always False.
        """
        state = await self.get_state(category)
        if state is None:
            logger.error("Cannot record success — category %s not found", category)
            return False, False

        success = await crud.record_success(self._db, state.id, updated_at=_now_iso())
        if not success:
            return False, False

        return True, False

    # ------------------------------------------------------------------
    # Event emission
    # ------------------------------------------------------------------

    def _emit_regression_event(
        self,
        category: str,
        level_before: int,
        level_after: int,
        corrected_at: str,
    ) -> None:
        """Emit an autonomy.regression event if event_bus is available."""
        if self._event_bus is None:
            return

        try:
            import contextlib

            from genesis.util.tasks import tracked_task

            coro = self._event_bus.emit(
                Subsystem.AUTONOMY,
                Severity.WARNING,
                "autonomy.regression",
                f"Autonomy level regressed for {category}: L{level_before} → L{level_after}",
                category=category,
                level_before=level_before,
                level_after=level_after,
                corrected_at=corrected_at,
            )
            with contextlib.suppress(RuntimeError):
                tracked_task(
                    coro, name="autonomy.regression-emit",
                    subsystem=Subsystem.AUTONOMY, logger=logger,
                )
        except Exception:
            logger.error(
                "Failed to emit autonomy.regression event", exc_info=True
            )

    def _emit_promotion_event(
        self, category: str, level_before: int, level_after: int,
    ) -> None:
        """Emit an autonomy.promotion event if event_bus is available."""
        if self._event_bus is None:
            return
        try:
            import contextlib

            from genesis.util.tasks import tracked_task

            coro = self._event_bus.emit(
                Subsystem.AUTONOMY,
                Severity.INFO,
                "autonomy.promotion",
                f"Autonomy level promoted for {category}: L{level_before} → L{level_after}",
                category=category,
                level_before=level_before,
                level_after=level_after,
            )
            with contextlib.suppress(RuntimeError):
                tracked_task(
                    coro, name="autonomy.promotion-emit",
                    subsystem=Subsystem.AUTONOMY, logger=logger,
                )
        except Exception:
            logger.error(
                "Failed to emit autonomy.promotion event", exc_info=True
            )
