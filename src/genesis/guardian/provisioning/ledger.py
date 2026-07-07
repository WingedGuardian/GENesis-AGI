"""Provisioning ledger + proposal state (atomic, 0600, never-raise).

``ledger.json`` records every EXECUTED mutation (a PUT was issued) — verified or
not — so the weekly rate cap counts real hypervisor changes, including ones that
didn't verify (an unverified mutation may well have landed). ``proposal_state.json``
holds the autonomous-propose damper timestamp so a sustained pool-crit doesn't
re-propose every tick.
"""

from __future__ import annotations

import json
import logging
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ProvisioningLedger:
    def __init__(self, state_dir: Path | str) -> None:
        self._dir = Path(state_dir).expanduser() / "provisioning"
        self._ledger = self._dir / "ledger.json"
        self._proposal = self._dir / "proposal_state.json"

    # ── io ────────────────────────────────────────────────────────────────
    def _read_json(self, path: Path, default: Any) -> Any:
        try:
            if not path.exists():
                return default
            return json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("provisioning ledger read failed (%s): %s", path, exc)
            return default

    def _atomic_write(self, path: Path, obj: Any) -> None:
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(json.dumps(obj, indent=2))
            os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)  # 0600
            os.replace(tmp, path)
        except OSError as exc:
            logger.warning("provisioning ledger write failed (%s): %s", path, exc)

    # ── mutations ledger ──────────────────────────────────────────────────
    def record_action(
        self, action: str, requested: str, ok: bool, verified: bool,
        target_bytes: int | None = None,
    ) -> None:
        """Append an executed mutation. Called only after a PUT was issued.

        ``target_bytes`` records the absolute size the mutation aimed for (disk
        grows only) so an unverified-but-landed grow can be detected later and
        not stacked with a second relative grow.
        """
        entries = self._read_json(self._ledger, [])
        if not isinstance(entries, list):
            entries = []
        entries.append({
            "ts": datetime.now(UTC).isoformat(),
            "action": action,
            "requested": requested,
            "ok": ok,
            "verified": verified,
            "target_bytes": target_bytes,
        })
        self._atomic_write(self._ledger, entries)

    def latest_unverified_disk(self, disk: str) -> dict | None:
        """The most recent disk-grow entry for ``disk`` IF it is unverified.

        Returns None when the latest grow for that disk is verified (or there is
        none) — i.e. no stacking risk. Used to detect a prior grow that may have
        landed after its verify re-read timed out, before issuing another.
        """
        entries = self._read_json(self._ledger, [])
        if not isinstance(entries, list):
            return None
        prefix = f"{disk} "
        for e in reversed(entries):
            if (e.get("action") == "grow_vm_disk"
                    and str(e.get("requested", "")).startswith(prefix)):
                return None if e.get("verified") else e
        return None

    def mark_latest_disk_verified(self, disk: str) -> None:
        """Flip the latest unverified disk-grow entry for ``disk`` to verified.

        Clears the stacking latch once we confirm (by a live size re-read) that
        a previously-unverified grow actually landed. Records NO new mutation,
        so it never counts against the rate cap.
        """
        entries = self._read_json(self._ledger, [])
        if not isinstance(entries, list):
            return
        prefix = f"{disk} "
        for e in reversed(entries):
            if (e.get("action") == "grow_vm_disk"
                    and str(e.get("requested", "")).startswith(prefix)):
                if not e.get("verified"):
                    e["verified"] = True
                    self._atomic_write(self._ledger, entries)
                return

    def actions_in_window(self, days: int = 7) -> int:
        """Count executed mutations within the rolling window."""
        entries = self._read_json(self._ledger, [])
        if not isinstance(entries, list):
            return 0
        cutoff = datetime.now(UTC) - timedelta(days=days)
        count = 0
        for e in entries:
            try:
                ts = datetime.fromisoformat(e["ts"])
            except (KeyError, TypeError, ValueError):
                continue
            if ts >= cutoff:
                count += 1
        return count

    # ── proposal damper ───────────────────────────────────────────────────
    def load_proposal_state(self) -> dict:
        state = self._read_json(self._proposal, {})
        return state if isinstance(state, dict) else {}

    def save_proposal_state(self, state: dict) -> None:
        self._atomic_write(self._proposal, state)

    def hours_since_last_proposal(self, key: str = "pool_grow") -> float | None:
        """Hours since the last autonomous proposal of ``key``, or None if never."""
        state = self.load_proposal_state()
        raw = state.get(key)
        if not raw:
            return None
        try:
            last = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return None
        return (datetime.now(UTC) - last).total_seconds() / 3600.0

    def mark_proposed(self, key: str = "pool_grow") -> None:
        state = self.load_proposal_state()
        state[key] = datetime.now(UTC).isoformat()
        self.save_proposal_state(state)
