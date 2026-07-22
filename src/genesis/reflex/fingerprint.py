"""Signal fingerprinting — pure functions, no I/O.

A fingerprint identifies "which exact way did which exact thing break" so
recurrences of one bug upsert into one ``reflex_signals`` row instead of N.

Inputs and exclusions:

- normalized task name — dynamic id fragments (uuid hex, counters) are
  scrubbed so occurrence 2 of ``obs-<uuid>`` matches occurrence 1
- exception type name
- normalized frame tail (rendered by ``genesis.util.tasks`` as
  ``relpath:funcname`` with NO line numbers — line numbers shift on every
  unrelated commit and would split fingerprints across deploys)
- the exception MESSAGE is deliberately excluded: it carries per-occurrence
  variable data (ids, values, timestamps)
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence

_HEX_RUN = re.compile(r"[0-9a-f]{8,}")
_DIGIT_RUN = re.compile(r"\d{4,}")


def normalize_task_name(name: str) -> str:
    """Scrub dynamic fragments from a task name so recurrences collapse."""
    norm = name.lower()
    norm = _HEX_RUN.sub("#", norm)
    return _DIGIT_RUN.sub("#", norm)


def fingerprint(task_name: str, error_type: str, frames: Sequence[str]) -> str:
    """Stable 16-hex-char identity for one failure mode of one task.

    Empty ``frames`` (event from a pre-upgrade process, or a traceback with
    no extractable frames) degrades to a coarser but still stable key.
    """
    key = f"{normalize_task_name(task_name)}|{error_type}|{'>'.join(frames)}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def derive_subsystem(frames: Sequence[str], fallback: str) -> str:
    """Failing subsystem = top-level package of the DEEPEST frame.

    Most ``tracked_task`` sites do not pass ``subsystem`` and default to
    HEALTH, which would collapse the ``class_key`` taxonomy axis; the frame
    path recovers it mechanically. Frames are outermost→innermost, so the
    last one is closest to the raise. A basename-only frame (no package
    path) yields ``fallback``.
    """
    if not frames:
        return fallback
    path = frames[-1].split(":", 1)[0]
    if "/" not in path:
        return fallback
    return path.split("/", 1)[0]


def class_key(error_type: str, subsystem: str) -> str:
    """Bug-class key, spec §7.2 sketch: ``<ErrorType>x<subsystem>``."""
    return f"{error_type}x{subsystem}"
