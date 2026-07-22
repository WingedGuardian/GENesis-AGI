"""Post-dispatch output verification for ego proposals.

After an ego-dispatched session completes, this module checks whether the
expected deliverables were produced. Verification is opt-in: proposals without
``expected_outputs`` metadata skip verification entirely.

**Success model — file existence is the only hard signal.** The trustworthy
"a real deliverable was produced" signal is: the expected file exists (after
``~``/env expansion and a fuzzy-name fallback) and is non-empty. Everything
else — ``min_size_bytes`` shortfalls and ``required_strings`` misses — is a
*weak proxy* and is recorded as an **advisory** only. A content/size check can
raise a note for the human, but it NEVER fails the proposal and never feeds a
negative learning signal: an expected string absent from a real deliverable is
a failure of the string matcher, not of the deliverable.

Hard failures (``missing_files``): the file is absent (no exact path, no fuzzy
match) or exists but is empty (0 bytes → not produced). Advisories: a non-empty
file under ``min_size_bytes``, or a ``required_strings`` entry not found
(matched case-insensitively and whitespace-normalized).

When an expected file is missing, a fuzzy-match fallback searches the parent
directory for similarly-named files (added suffixes, version numbers, slight
name variations) without false-positiving on unrelated files.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExpectedOutputs:
    """Structured verification criteria for dispatch deliverables."""

    files: list[str]
    min_size_bytes: int = 0
    required_strings: list[str] = field(default_factory=list)


@dataclass
class VerificationResult:
    """Outcome of a post-dispatch verification check.

    ``missing_files`` are hard failures (deliverable not produced) and drive
    ``passed``. ``advisories`` are non-fatal notes (size/content proxies) that
    are surfaced to the human but never fail the proposal.
    """

    passed: bool
    missing_files: list[str] = field(default_factory=list)
    advisories: list[str] = field(default_factory=list)


def parse_expected_outputs(raw: str | None) -> ExpectedOutputs | None:
    """Parse an ``expected_outputs`` JSON string into a typed object.

    Returns ``None`` if the input is absent, empty, or malformed JSON.
    This is the backward-compatibility gate: proposals without
    ``expected_outputs`` simply skip verification.
    """
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    files = data.get("files")
    if not files or not isinstance(files, list):
        return None
    return ExpectedOutputs(
        files=[str(f) for f in files],
        min_size_bytes=int(data.get("min_size_bytes", 0)),
        required_strings=list(data.get("required_strings") or []),
    )


def _resolve_path(filepath: str) -> Path:
    """Expand ``~`` and ``$VARS`` before building a Path.

    ``Path("~/x.md").exists()`` is always False (Python does not expand ``~``),
    which used to produce spurious 'Missing file' hard-fails on deliverables
    written to the expanded home path. Expand env vars first, then ``~``.
    """
    return Path(os.path.expanduser(os.path.expandvars(filepath)))


def _normalize(text: str) -> str:
    """Collapse all whitespace runs to single spaces and casefold.

    Makes required-string matching forgiving of casing and incidental
    whitespace/newline differences — the common ways a real deliverable states
    the same content without the exact literal.
    """
    return " ".join(text.split()).casefold()


def _content_has(content: str, required: str) -> bool:
    """True if *required* appears in *content*, ignoring case and whitespace."""
    return _normalize(required) in _normalize(content)


def _find_similar(expected: Path, *, min_ratio: float = 0.6) -> Path | None:
    """Fuzzy-match against files in the same directory.

    Searches the parent directory for files with the same extension whose
    stem is similar to the expected filename.  When multiple candidates
    match, prefers higher similarity; ties broken by most-recently-modified
    (to avoid stale artifacts from previous dispatches).

    Returns the best match, or ``None`` if nothing scores above *min_ratio*.
    """
    parent = expected.parent
    if not parent.is_dir():
        return None

    best: Path | None = None
    best_score = 0.0
    best_mtime = 0.0

    for candidate in parent.iterdir():
        if not candidate.is_file() or candidate.suffix != expected.suffix:
            continue
        ratio = SequenceMatcher(None, expected.stem, candidate.stem).ratio()
        if ratio < min_ratio:
            continue
        mtime = candidate.stat().st_mtime
        # Higher ratio wins; same ratio → most recently modified wins
        if ratio > best_score or (ratio == best_score and mtime > best_mtime):
            best_score = ratio
            best_mtime = mtime
            best = candidate

    if best is not None:
        logger.warning(
            "Fuzzy match: expected %s → found %s (ratio=%.2f)",
            expected.name,
            best.name,
            best_score,
        )
    return best


def verify_outputs(expected: ExpectedOutputs) -> VerificationResult:
    """Check deliverable existence (hard) plus size/content (advisory).

    A file that is absent (no exact path after ``~``/env expansion, no fuzzy
    match) or empty (0 bytes) is a hard failure recorded in ``missing_files``
    and fails the result. A non-empty file under ``min_size_bytes`` and any
    unmatched ``required_strings`` are recorded in ``advisories`` and do NOT
    fail the result — string/size are weak proxies for "did it satisfy intent",
    so they inform but never gate.

    This is synchronous filesystem I/O — callers in async contexts should
    wrap in ``asyncio.to_thread`` if latency is a concern. In practice this
    runs after a multi-minute CC session, so the overhead is negligible.
    """
    missing_files: list[str] = []
    advisories: list[str] = []

    for filepath in expected.files:
        path = _resolve_path(filepath)
        fuzzy = False
        # is_file() (not exists()) so a directory at the expected path is not
        # mistaken for a deliverable (a dir's st_size reads as non-zero).
        if not path.is_file():
            # Fuzzy fallback: search parent directory for similar files
            similar = _find_similar(path)
            if similar is not None:
                path = similar
                fuzzy = True
            else:
                missing_files.append(f"Missing file: {filepath}")
                continue
        # Use the resolved path in messages so operators can find the file
        label = f"{path} (fuzzy match for {filepath})" if fuzzy else filepath

        # A fuzzy match means the EXACT expected path was absent. No heuristic
        # (name-similarity OR content) reliably tells a rename from an unrelated
        # similarly-named file, so we still count it as produced (never a
        # false-fail on a real rename) but flag it LOUDLY — a possibly-wrong
        # file must never pass silently. The LLM semantic verifier is the real
        # disambiguation.
        if fuzzy:
            advisories.append(
                f"Fuzzy filename match — expected {filepath!r}, used "
                f"{path.name!r}; verify this is the intended deliverable"
            )

        try:
            size = path.stat().st_size
        except OSError as exc:
            missing_files.append(f"Cannot stat {label}: {exc}")
            continue

        # A 0-byte file is "not produced" — a hard failure.
        if size == 0:
            missing_files.append(f"Empty file (0 bytes): {label}")
            continue

        # Size shortfall is a weak proxy → advisory only.
        if size < expected.min_size_bytes:
            advisories.append(
                f"File smaller than expected: {label} ({size}B < {expected.min_size_bytes}B)"
            )

        if expected.required_strings:
            try:
                content = path.read_text(errors="replace")
            except OSError as exc:
                advisories.append(f"Cannot read {label}: {exc}")
                continue
            for req in expected.required_strings:
                if not _content_has(content, req):
                    advisories.append(
                        f"Content hint not found verbatim in {label}: "
                        f"{req!r} — deliverable accepted"
                    )

    return VerificationResult(
        passed=len(missing_files) == 0,
        missing_files=missing_files,
        advisories=advisories,
    )
