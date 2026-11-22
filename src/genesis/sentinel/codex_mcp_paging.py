"""Confirm Codex tool-process retention from repeated post-unload samples."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.sentinel.codex_mcp_inventory import Inventory

POST_UNLOAD_GRACE_S = 60
STALE_AFTER_S = 120


class Eligibility(StrEnum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNKNOWN = "unknown"


@dataclass
class RetentionTracker:
    daemon: tuple[str, int, int] | None = None
    baseline_threads: frozenset[str] = frozenset()
    baseline_groups: frozenset[tuple[int, int]] = frozenset()
    confirmed_groups: frozenset[tuple[int, int]] = frozenset()
    expanded_groups: frozenset[tuple[int, int]] = frozenset()
    pending_groups: frozenset[tuple[int, int]] = frozenset()
    excursions: int = 0
    expanded: bool = False
    returned_at: float | None = None
    observed_at: float | None = None
    complete: bool = False
    baseline_held_kib: int = 0
    confirmed_held_kib: int = 0
    held_kib: int = 0

    def _clear_excursion(self) -> None:
        self.expanded = False
        self.returned_at = None
        self.expanded_groups = self.pending_groups = frozenset()

    def observe(self, sample: Inventory, now: float) -> None:
        if self.observed_at is not None and now <= self.observed_at:
            return
        if sample.status != "complete" or sample.daemon is None:
            self.daemon = None
            self.baseline_threads = frozenset()
            self.baseline_groups = self.confirmed_groups = frozenset()
            self.excursions = 0
            self._clear_excursion()
            self.complete = sample.status == "absent"
            self.observed_at = now
            self.baseline_held_kib = self.confirmed_held_kib = self.held_kib = 0
            return

        if (
            sample.daemon != self.daemon
            or not self.baseline_threads <= sample.threads
            or not self.confirmed_groups <= sample.groups
            or (self.observed_at is not None and now - self.observed_at > STALE_AFTER_S)
        ):
            self.daemon = sample.daemon
            self.baseline_threads = sample.threads
            self.baseline_groups = self.confirmed_groups = sample.groups
            self.baseline_held_kib = sample.held_kib
            self.confirmed_held_kib = 0
            self.excursions = 0
            self._clear_excursion()
        self.complete = True
        self.observed_at = now
        self.held_kib = sample.held_kib

        if sample.threads > self.baseline_threads:
            if self.returned_at is not None:
                self.expanded_groups = frozenset()
            self.expanded_groups |= sample.groups - self.confirmed_groups
            self.expanded = True
            self.returned_at = None
            self.pending_groups = frozenset()
            return
        if not self.expanded:
            return
        if self.returned_at is None:
            self.returned_at = now
            return
        if now - self.returned_at < POST_UNLOAD_GRACE_S:
            return
        residual = self.expanded_groups & sample.groups
        if not residual:
            self._clear_excursion()
            self.excursions = 0
            self.confirmed_held_kib = 0
            return
        if self.pending_groups != residual:
            self.pending_groups = residual
            return
        self.confirmed_groups |= residual
        self.confirmed_held_kib = sample.held_kib
        self.excursions += 1
        self._clear_excursion()

    def eligibility(self, now: float, threshold_mb: int | None) -> Eligibility:
        if not self.complete or self.observed_at is None or now - self.observed_at > STALE_AFTER_S:
            return Eligibility.UNKNOWN
        if threshold_mb and min(self.held_kib, self.confirmed_held_kib) >= threshold_mb * 1024 and self.excursions >= 2:
            return Eligibility.ELIGIBLE
        return Eligibility.INELIGIBLE


_tracker = RetentionTracker()


def observe(sample: Inventory, threshold_mb: int | None) -> dict:
    now = time.monotonic()
    _tracker.observe(sample, now)
    threshold_crossed = False
    if threshold_mb:
        if sample.scan_held_kib is not None and sample.scan_held_kib >= threshold_mb * 1024:
            threshold_crossed = True
        elif not sample.scan_complete:
            threshold_crossed = None
    return {
        "eligibility": _tracker.eligibility(now, threshold_mb).value,
        "legacy_threshold_crossed": threshold_crossed,
        "completed_growth_cycles": _tracker.excursions,
        "baseline_groups": len(_tracker.baseline_groups),
        "baseline_held_kib": _tracker.baseline_held_kib,
        "confirmed_held_kib": _tracker.confirmed_held_kib,
        "residual_groups": len(_tracker.confirmed_groups - _tracker.baseline_groups),
    }
