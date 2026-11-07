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

import errno
import json
import logging
import math
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
_TEXT_FIELDS = ("title", "body", "severity", "source")
# Meta keys the host drain reads (guardian/check.py _alert_from_queue_entry).
_HOST_META_KEYS = ("likely_cause", "proposed_action", "approval_url")


def _finite(value: float) -> bool:
    try:
        return math.isfinite(value)
    except OverflowError:  # an int too large for a float
        return False


def v1_entry_problem(entry: Any) -> str | None:
    """Name the field that makes ``entry`` unusable as a schema-v1 alert, or
    return None when every reader can use it. Never raises.

    The readers fail on more than bad JSON: a non-numeric ``ts`` raises in the
    oldest-first sort, a non-str title raises in the host's HTML escaping, a
    non-dict ``meta`` raises in the host's ``meta.get``. Any of those wedges the
    drain head every tick. One rule is policy rather than a crash guard: an
    entry with neither title nor body is a blank page, never delivered. Mirrored in scripts/lib/alert_queue.sh (its dedupe
    scan runs under ``python3 -I -S`` and cannot import this module); keep the
    two in step — tests/test_scripts/test_alert_queue_sh.py checks parity.
    """
    if not isinstance(entry, dict):
        return "entry"
    schema = entry.get("schema")
    if type(schema) is not int or schema != SCHEMA_VERSION:
        return "schema"
    ts = entry.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or not _finite(ts):
        return "ts"
    for name in _TEXT_FIELDS:
        if name in entry and not isinstance(entry[name], str):
            return name
    if not (entry.get("title") or entry.get("body")):
        return "title"
    dedupe_key = entry.get("dedupe_key")
    if dedupe_key is not None and not isinstance(dedupe_key, str):
        return "dedupe_key"
    meta = entry.get("meta")
    if meta is not None and not isinstance(meta, dict):
        return "meta"
    for name in _HOST_META_KEYS:
        value = (meta or {}).get(name)
        if value is not None and not isinstance(value, str):
            return f"meta.{name}"
    return None


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

    A file that is not a usable v1 entry (``v1_entry_problem``) is renamed
    ``<name>.corrupt``, logged, and skipped, so one bad write can never wedge the
    drain, the prune or the enqueue-time dedupe scan.
    """
    root = Path(root).expanduser()
    if not root.exists():
        return []
    out: list[tuple[float, str, Path, dict[str, Any]]] = []
    for path in root.glob("*.json"):
        try:
            entry = json.loads(path.read_text())
            problem = v1_entry_problem(entry)
            if problem is not None:
                raise ValueError(f"not a v1 alert entry: bad {problem}")
            ts = float(entry["ts"])
        except FileNotFoundError:
            continue  # drained or quarantined by another reader meanwhile
        except OSError as exc:
            if isinstance(exc, (IsADirectoryError, NotADirectoryError)) or exc.errno == errno.ELOOP:
                # Not a file at all: no retry will ever read it, so quarantine
                # it like bad content (review finding: kept, it warned forever).
                logger.warning("alert-queue entry %s quarantined (%s)", path.name, exc)
                _quarantine_corrupt(path)
                continue
            # Unreadable right now (permissions, fd limit, I/O): the content may
            # be fine, so keep it for the next pass rather than quarantine it.
            logger.warning("alert-queue entry %s unreadable, kept (%s)", path.name, exc)
            continue
        except Exception as exc:
            logger.warning("alert-queue entry %s quarantined (%s)", path.name, exc)
            _quarantine_corrupt(path)
            continue
        out.append((ts, path.name, path, entry))
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
    ``max_files``. Quarantined ``*.corrupt`` files (nothing reads them) get the
    same two bounds: older than ``max_age_s``, then oldest over ``max_files``, so
    a producer that keeps writing bad entries cannot grow them without limit
    (review finding). A quarantined directory is never removed here.
    Best-effort — returns the number removed, never raises.
    """
    removed = 0
    try:
        entries = list_queued(root)
        now = time.time()
        kept_corrupt: list[tuple[float, str, Path]] = []
        for path in Path(root).expanduser().glob("*.corrupt"):
            try:
                st = path.lstat()
                if not os.path.isfile(path) or os.path.islink(path):
                    continue
                if now - st.st_mtime > max_age_s:
                    removed += _safe_unlink(path)
                else:
                    kept_corrupt.append((st.st_mtime, path.name, path))
            except OSError:
                continue
        kept_corrupt.sort()
        for _, _, path in kept_corrupt[: max(0, len(kept_corrupt) - max_files)]:
            removed += _safe_unlink(path)
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
