#!/usr/bin/env python3
"""CI private-pattern exact scan — the hard leak gate for the public repo.

Reads install-specific EXACT patterns (from the ``GENESIS_PRIVATE_PATTERNS``
repo secret, written to a file by the workflow) and scans a haystack — the
PR/push added diff lines + PR body, on stdin — for any match. Zero such values
are tracked in the repo; they live only in this secret and the local fingerprint
file.

This is the tested replacement for the former inline-shell gate, which
accumulated several fail-OPEN footguns that only a live CI run would surface:
  - a blank line in the ``grep -E -f`` pattern file is an EMPTY regex that
    matches every line (secret's trailing newline) -> gate fails on every PR;
  - added content beginning ``++`` renders as ``+++...`` and was dropped by the
    ``+++`` diff-header filter -> such a line escaped the scan;
  - ``grep`` exit code 2 (a malformed ERE in the secret) was read as "no match"
    -> the gate passed green on a corrupt pattern.
Doing the match in Python makes every one of those edge cases unit-testable.

CONTRACT — never emit matched CONTENT (only counts and 1-based pattern indices):
  exit 0  clean — no pattern matched
  exit 1  leak — at least one pattern matched; prints ``match count: N``
  exit 3  provisioning error — no usable patterns, or a pattern failed to
          compile; fail-LOUD so a missing/corrupt secret can never disable the
          gate silently

Blank lines and ``#`` comments in the pattern file are ignored, mirroring
``sanitize._check_fingerprints`` and ``scripts/hooks/commit-msg``.
"""

from __future__ import annotations

import argparse
import re
import sys

EXIT_CLEAN = 0
EXIT_LEAK = 1
EXIT_PROVISIONING = 3


class PatternError(ValueError):
    """A pattern file is unusable (empty after filtering, or a bad regex)."""


def load_patterns(text: str) -> list[re.Pattern[str]]:
    """Compile the usable patterns from raw pattern-file text.

    Blank and ``#``-comment lines are ignored. Raises :class:`PatternError` if
    no usable patterns remain or any line is not a valid regex — the error names
    the 1-based line number only, NEVER the pattern content (it is a secret).
    """
    patterns: list[re.Pattern[str]] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            patterns.append(re.compile(stripped))
        except re.error as exc:
            raise PatternError(
                f"pattern on line {idx} is not a valid regex ({exc.__class__.__name__})"
            ) from exc
    if not patterns:
        raise PatternError("no usable patterns (all blank/comment)")
    return patterns


def count_matching_lines(haystack: str, patterns: list[re.Pattern[str]]) -> int:
    """Return how many haystack LINES match at least one pattern.

    Line-based and content-safe: the matched text is never returned or logged.
    """
    return sum(1 for line in haystack.splitlines() if any(p.search(line) for p in patterns))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CI private-pattern exact leak gate")
    ap.add_argument(
        "--patterns",
        required=True,
        help="path to the pattern file (the GENESIS_PRIVATE_PATTERNS secret)",
    )
    args = ap.parse_args(argv)

    try:
        with open(args.patterns, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        print(
            f"::error::cannot read pattern file ({exc.__class__.__name__})",
            file=sys.stderr,
        )
        return EXIT_PROVISIONING

    try:
        patterns = load_patterns(raw)
    except PatternError as exc:
        print(
            f"::error::private pattern layer unusable — {exc}. Regenerate with "
            "GENESIS_SYNC_PRIVATE_PATTERNS=1",
            file=sys.stderr,
        )
        return EXIT_PROVISIONING

    haystack = sys.stdin.read()
    n = count_matching_lines(haystack, patterns)
    if n:
        print(
            "::error::private install identifier present in PR/push additions or "
            "PR body — content withheld"
        )
        print(f"match count: {n}")
        return EXIT_LEAK
    print("Private-pattern exact scan: CLEAN")
    return EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
