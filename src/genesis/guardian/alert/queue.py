"""Durable alert queue — store-and-forward when the alert transport is down.

The reliability floor under every other resiliency signal: an alert raised while
Telegram is unreachable is persisted to disk and delivered on the next drain,
instead of vanishing into a log line (the failure mode `backup.sh::_send_telegram`
and the host `AlertDispatcher` both had).

Per-side topology (F.3) — each side drains its OWN queue, never a shared mount
(that would invert reliability and race on delivery):
  - Host guardian: ``config.state_path/"alerts"/"queue"`` — drained at the top of
    ``run_check`` (the 30s tick is the best drainer for host→Telegram outages).
  - Container: ``~/.genesis/alerts/queue`` — drained on the awareness tick.
    Lives in a ``queue/`` SUBDIR so it never collides with ``tmp_watchgod``'s
    ``tmp_warning`` / ``tmp_emergency`` flag files in ``~/.genesis/alerts/``.

This module is deliberately **dependency-free** (stdlib only, no ``genesis``
imports): it operates on plain dict entries, so callers own the mapping to their
transport (host → ``Alert``/``dispatcher``; container → ``OutreachRequest``/
``submit_raw``). That keeps it unit-testable in isolation and lets shell scripts
write the same schema via ``scripts/lib/alert_queue.sh`` without importing it.

Schema v1 (one JSON file per alert, named ``<ts>-<uuid>.json``)::

    {"schema": 1, "ts": <float>, "severity": str, "source": str,
     "title": str, "body": str, "dedupe_key": str|null, "meta": {...}}
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
_MAX_PER_RUN = 10
_PRUNE_MAX_FILES = 200
_PRUNE_MAX_AGE_S = 14 * 24 * 3600  # 14 days
_DEDUPE_SCAN_LIMIT = 200  # bound the enqueue-time dedupe scan on a huge backlog


def enqueue_alert(
    root: Path | str,
    *,
    severity: str,
    source: str,
    title: str,
    body: str,
    dedupe_key: str | None = None,
    meta: dict[str, Any] | None = None,
) -> bool:
    """Persist one alert to the queue. Best-effort — **never raises**.

    Durability must never break its caller (a guardian tick, a dispatcher send,
    a backup run): any failure is logged and swallowed, returning ``False``.

    When ``dedupe_key`` is set and a live (undrained) entry already carries it,
    the write is skipped and ``False`` is returned — so a condition that recurs
    every tick while the channel is down leaves at most ONE queued entry per key.
    """
    try:
        root = Path(root).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        if dedupe_key and _has_live_dedupe_key(root, dedupe_key):
            return False
        ts = time.time()
        entry = {
            "schema": SCHEMA_VERSION,
            "ts": ts,
            "severity": severity,
            "source": source,
            "title": title,
            "body": body,
            "dedupe_key": dedupe_key,
            "meta": meta or {},
        }
        _atomic_write_json(root / f"{ts:.6f}-{uuid.uuid4().hex}.json", entry)
        return True
    except Exception:
        logger.warning("enqueue_alert failed — alert dropped from queue", exc_info=True)
        return False


def list_queued(root: Path | str) -> list[tuple[Path, dict[str, Any]]]:
    """Return ``(path, entry)`` pairs oldest-first (by ``ts``, then filename).

    A file that cannot be parsed as a v1 entry is renamed ``<name>.corrupt`` and
    skipped, so one bad write can never wedge the drain.
    """
    root = Path(root).expanduser()
    if not root.exists():
        return []
    out: list[tuple[float, str, Path, dict[str, Any]]] = []
    for path in root.glob("*.json"):
        try:
            entry = json.loads(path.read_text())
            if not isinstance(entry, dict) or "ts" not in entry:
                raise ValueError("not a v1 alert entry")
        except Exception:
            _quarantine_corrupt(path)
            continue
        out.append((float(entry.get("ts", 0.0)), path.name, path, entry))
    out.sort(key=lambda t: (t[0], t[1]))
    return [(p, e) for _, _, p, e in out]


async def drain(
    root: Path | str,
    send: Callable[[dict[str, Any]], Awaitable[bool]],
    *,
    max_per_run: int = _MAX_PER_RUN,
) -> int:
    """Deliver queued alerts oldest-first via ``send``. Returns the count removed.

    ``send(entry) -> bool`` contract:
      - ``True``  = **terminal** — the alert was delivered OR intentionally
        dropped (e.g. the outreach pipeline's dedup returned ``REJECTED``). The
        entry is unlinked. (REJECTED is NOT a failure to retry — treating it as
        one would leave a deduped entry stuck in the queue forever.)
      - ``False`` = **transient failure** — the channel is still down. The entry
        is kept and the drain STOPS for this run (no point burning the batch
        against a dead channel; the next tick retries from the same head).

    ``send`` raising is treated as a transient failure (kept, stop).
    """
    removed = 0
    for path, entry in list_queued(root)[:max_per_run]:
        try:
            terminal = await send(entry)
        except Exception:
            logger.warning("alert-queue send raised — keeping entry", exc_info=True)
            break
        if not terminal:
            break
        removed += _safe_unlink(path)
    return removed


def prune(
    root: Path | str,
    *,
    max_files: int = _PRUNE_MAX_FILES,
    max_age_s: float = _PRUNE_MAX_AGE_S,
) -> int:
    """Bound the queue: drop entries older than ``max_age_s``, then oldest over
    ``max_files``. Best-effort — returns the number removed, never raises.
    """
    removed = 0
    try:
        entries = list_queued(root)
        now = time.time()
        survivors: list[tuple[Path, dict[str, Any]]] = []
        for path, entry in entries:
            if now - float(entry.get("ts", now)) > max_age_s:
                removed += _safe_unlink(path)
            else:
                survivors.append((path, entry))
        overflow = len(survivors) - max_files
        for path, _ in survivors[: max(0, overflow)]:  # oldest-first already
            removed += _safe_unlink(path)
    except Exception:
        logger.warning("alert-queue prune failed", exc_info=True)
    return removed


# --- internals ---------------------------------------------------------------


def _has_live_dedupe_key(root: Path, dedupe_key: str) -> bool:
    for i, (_, entry) in enumerate(list_queued(root)):
        if i >= _DEDUPE_SCAN_LIMIT:
            break
        if entry.get("dedupe_key") == dedupe_key:
            return True
    return False


def _atomic_write_json(path: Path, obj: dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)
    os.replace(tmp, path)


def _quarantine_corrupt(path: Path) -> None:
    try:
        path.rename(path.with_suffix(path.suffix + ".corrupt"))
    except Exception:
        logger.debug("could not quarantine corrupt alert entry %s", path, exc_info=True)


def _safe_unlink(path: Path) -> int:
    try:
        path.unlink()
        return 1
    except FileNotFoundError:
        return 0
    except Exception:
        logger.debug("could not unlink alert entry %s", path, exc_info=True)
        return 0
