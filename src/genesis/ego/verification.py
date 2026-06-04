"""Post-dispatch output verification for ego proposals.

After an ego-dispatched session completes, this module checks whether
the expected deliverables actually exist and meet minimum criteria.
Verification is opt-in: proposals without ``expected_outputs`` metadata
skip verification entirely.

When an expected file is missing, a fuzzy-match fallback searches the
parent directory for similarly-named files (using SequenceMatcher).
This catches common dispatch mismatches (added suffixes, version numbers,
slight name variations) without false-positiving on unrelated files.
"""

from __future__ import annotations

import json
import logging
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
    """Outcome of a post-dispatch verification check."""

    passed: bool
    failures: list[str] = field(default_factory=list)


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
    """Check file existence, size, and required content strings.

    When an expected file is missing, attempts a fuzzy match against
    similarly-named files in the same directory.  If a match is found,
    verification continues against that file (size + content checks)
    and the result is treated as a pass with a logged warning.

    This is synchronous filesystem I/O — callers in async contexts should
    wrap in ``asyncio.to_thread`` if latency is a concern. In practice
    this runs after a multi-minute CC session, so the overhead is
    negligible.
    """
    failures: list[str] = []

    for filepath in expected.files:
        path = Path(filepath)
        fuzzy = False
        if not path.exists():
            # Fuzzy fallback: search parent directory for similar files
            similar = _find_similar(path)
            if similar is not None:
                path = similar
                fuzzy = True
            else:
                failures.append(f"Missing file: {filepath}")
                continue
        # Use the resolved path in failure messages so operators can find the file
        label = f"{path} (fuzzy match for {filepath})" if fuzzy else filepath
        size = path.stat().st_size
        if size < expected.min_size_bytes:
            failures.append(
                f"File too small: {label} ({size}B < {expected.min_size_bytes}B)"
            )
        if expected.required_strings:
            try:
                content = path.read_text(errors="replace")
                for req in expected.required_strings:
                    if req not in content:
                        failures.append(
                            f"Missing required string in {label}: {req!r}"
                        )
            except OSError as exc:
                failures.append(f"Cannot read {label}: {exc}")

    return VerificationResult(passed=len(failures) == 0, failures=failures)
