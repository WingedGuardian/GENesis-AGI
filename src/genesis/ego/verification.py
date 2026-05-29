"""Post-dispatch output verification for ego proposals.

After an ego-dispatched session completes, this module checks whether
the expected deliverables actually exist and meet minimum criteria.
Verification is opt-in: proposals without ``expected_outputs`` metadata
skip verification entirely.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
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


def verify_outputs(expected: ExpectedOutputs) -> VerificationResult:
    """Check file existence, size, and required content strings.

    This is synchronous filesystem I/O — callers in async contexts should
    wrap in ``asyncio.to_thread`` if latency is a concern. In practice
    this runs after a multi-minute CC session, so the overhead is
    negligible.
    """
    failures: list[str] = []

    for filepath in expected.files:
        path = Path(filepath)
        if not path.exists():
            failures.append(f"Missing file: {filepath}")
            continue
        size = path.stat().st_size
        if size < expected.min_size_bytes:
            failures.append(
                f"File too small: {filepath} ({size}B < {expected.min_size_bytes}B)"
            )
            continue
        if expected.required_strings:
            try:
                content = path.read_text(errors="replace")
                for req in expected.required_strings:
                    if req not in content:
                        failures.append(
                            f"Missing required string in {filepath}: {req!r}"
                        )
            except OSError as exc:
                failures.append(f"Cannot read {filepath}: {exc}")

    return VerificationResult(passed=len(failures) == 0, failures=failures)
