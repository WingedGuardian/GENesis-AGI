"""Whole-session lesson builder lever: ``mode: off | shadow | live``, read live.

Re-read from the merged YAML (``config/lesson_builder.yaml`` + the user overlay
``~/.genesis/config/lesson_builder.local.yaml``) on every extraction cycle, so a
flip takes effect without a restart. An invalid value degrades to ``shadow``,
which stores nothing (the same fail-safe as ``genesis.ledger.ws2_ledger_config``).

In ``shadow`` the builder runs its real model call and its scoping and novelty
checks, then appends each lesson it would have stored to
``~/.genesis/output/lesson_builder_shadow.jsonl`` (:func:`record_shadow_lesson`)
instead of storing it.

Dependency rule: stdlib + yaml + genesis.env + genesis._config_overlay only;
``genesis.mcp.health.settings`` imports MODES from here, never the reverse.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from genesis._config_overlay import merge_local_overlay
from genesis.env import output_dir, repo_root

logger = logging.getLogger(__name__)

MODES = ("off", "shadow", "live")

_CONFIG_NAME = "lesson_builder.yaml"

SHADOW_FILE = "lesson_builder_shadow.jsonl"

# The per-session lesson cap counts over this rolling window. A session that
# stays active across many 2-hour cycles must not get a fresh cap each cycle.
# The shadow record rotates on it too (see record_shadow_lesson).
CAP_WINDOW = timedelta(hours=24)

DEFAULTS: dict[str, Any] = {"mode": "live", "segment": "shadow"}

# How much of a session the builder judges (#4807):
#   whole: the whole session, as before; no progress marker, no shadow line.
#   shadow: the whole session, plus one log line per built session saying what
#     judging only the unjudged part would do, and the progress marker
#     (cc_sessions.lesson_built_through) moved as that rule would move it.
#   incremental: judge only the unjudged part (up to 4 parts per pass; the
#     rebuild drain and the cycle each get a pass; leftovers and failures queue
#     a rebuild that continues from the
#     marker). Needs mode live; under shadow or off it runs as shadow.
SEGMENTS = ("whole", "shadow", "incremental")


def _base_path() -> Path:
    return repo_root() / "config" / _CONFIG_NAME


def load_config() -> dict[str, Any]:
    """Read the merged config fresh, per call (defaults, base yaml, overlay).

    The shipped base is ``live``, so a read failure must not fall back to it: an
    unreadable base, a base whose root is not a mapping (an empty document
    included: the shipped file is never empty), or a malformed overlay (an owner
    pausing the builder with a typo) reads as ``shadow``, which stores nothing.
    """
    base_path = _base_path()
    try:
        loaded = yaml.safe_load(base_path.read_text())
        if not isinstance(loaded, dict):
            raise ValueError(f"top level is {type(loaded).__name__}, not a mapping")
        merged = {**DEFAULTS, **merge_local_overlay(loaded, base_path, strict=True)}
    except Exception:
        logger.warning(
            "lesson_builder config or its overlay unreadable (base %s) — reading as shadow",
            base_path, exc_info=True,
        )
        return {"mode": "shadow", "segment": "shadow"}
    return merged


def builder_mode() -> str:
    """The lesson builder mode. An invalid value degrades to ``shadow``."""
    mode = load_config().get("mode")
    if mode is False:
        # An unquoted `mode: off` parses as YAML-1.1 boolean False.
        return "off"
    if mode not in MODES:
        logger.warning("lesson_builder has invalid mode %r — degrading to shadow", mode)
        return "shadow"
    return mode


def segment_mode() -> str:
    """How much of a session the builder judges (see :data:`SEGMENTS`).

    An invalid value reads as ``shadow``, which changes nothing the builder
    stores. ``incremental`` takes effect only while the builder ``mode`` is
    ``live``; the extraction job runs it as ``shadow`` otherwise.
    """
    segment = load_config().get("segment")
    if segment not in SEGMENTS:
        key = repr(segment)
        if key not in _SEGMENT_WARNED:  # once per value per process, not per session
            _SEGMENT_WARNED.add(key)
            logger.warning("lesson_builder segment %s is invalid — reading as shadow", key)
        return "shadow"
    return segment


_SEGMENT_WARNED: set[str] = set()


def _records(path: Path) -> list[dict[str, Any]]:
    """The parseable records of one shadow file, oldest first; [] if unreadable.

    Split on newline only: records are written with ``ensure_ascii=False``, so a
    lesson's text can hold U+2028 or U+0085 raw, which ``splitlines`` would cut.
    """
    try:
        lines = path.read_text(encoding="utf-8").split("\n")
    except (OSError, ValueError):
        return []
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
            datetime.fromisoformat(entry["ts"])
        except (ValueError, TypeError, KeyError, AttributeError):
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _oldest_ts(path: Path) -> datetime | None:
    """The first parseable record's time in ``path``, or None."""
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                try:
                    return datetime.fromisoformat(json.loads(line)["ts"])
                except (ValueError, TypeError, KeyError, AttributeError):
                    continue
    except (OSError, ValueError):
        return None
    return None


def _ends_mid_line(path: Path) -> bool:
    """True when ``path`` is non-empty and its last byte is not a newline."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            if f.tell() == 0:
                return False
            f.seek(-1, 2)
            return f.read(1) != b"\n"
    except OSError:
        return False


def record_shadow_lesson(entry: dict[str, Any]) -> None:
    """Append one would-be lesson to the shadow record. Never raises.

    The record is the cap's count of shadow lessons, so it rotates by time, not
    size: the active file moves to ``.1`` (replacing it) only once its oldest
    record is older than :data:`CAP_WINDOW`. Every record it then drops from the
    old ``.1`` is older still, so the two files always hold the whole window.
    Their size is bounded by two windows of lessons, which the cap bounds. Each
    record carries a principle embedding (about 5.5 KB of base64); at the
    builder's highest uncapped rate on one live install (119 lessons in a day,
    2026-11) two windows come to about 1.6 MB.
    """
    path = output_dir() / SHADOW_FILE
    now = datetime.now(UTC)
    line = json.dumps({"ts": now.isoformat(), **entry}, ensure_ascii=False)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _locked(path, exclusive=True):
            oldest = _oldest_ts(path)
            if oldest is not None and oldest.tzinfo is not None and now - oldest > CAP_WINDOW:
                path.replace(path.with_name(path.name + ".1"))
            # A record cut short (a crash, a full disk) leaves no newline; start a
            # fresh line so it cannot swallow this one.
            lead = "\n" if _ends_mid_line(path) else ""
            with path.open("a", encoding="utf-8") as f:
                f.write(lead + line + "\n")
    except Exception:
        logger.warning("lesson_builder shadow record write failed at %s", path, exc_info=True)


@contextlib.contextmanager
def _locked(path: Path, *, exclusive: bool) -> Iterator[None]:
    """Hold ``<record>.lock`` across a rotate-and-append, or across reading both
    files, so two processes (the server and a history-mining run) cannot rotate
    twice and drop records still in the window, and a reader never sees the
    active file moved between its two reads. When the lock file cannot be opened
    (no directory yet, nothing recorded), proceed unlocked."""
    try:
        handle = open(path.with_name(path.name + ".lock"), "a")  # noqa: SIM115
    except OSError:
        yield
        return
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        except OSError:
            logger.warning("lesson_builder shadow lock unavailable — proceeding unlocked", exc_info=True)
        yield
    finally:
        handle.close()  # closing releases the lock


def shadow_records(session_id: str, since: datetime) -> list[dict[str, Any]]:
    """Shadow-recorded lessons for ``session_id`` at or after ``since``.

    Reads the rotated ``.1`` and the current record, under the record's shared
    lock. Unreadable files and malformed lines count as nothing.
    """
    path = output_dir() / SHADOW_FILE
    out = []
    with _locked(path, exclusive=False):
        files = [_records(path.with_name(path.name + ".1")), _records(path)]
    for records in files:
        for entry in records:
            try:
                if entry.get("session_id") == session_id and (
                    datetime.fromisoformat(entry["ts"]) >= since
                ):
                    out.append(entry)
            except TypeError:  # a naive timestamp cannot be compared with ``since``
                continue
    return out


def shadow_count(session_id: str, since: datetime) -> int:
    """How many lessons :func:`shadow_records` holds for the session."""
    return len(shadow_records(session_id, since))
