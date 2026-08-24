#!/usr/bin/env python3
"""Shared-artifact consumer-drift guard — a registered artifact's documented
consumer set must match the code that actually references it.

Some artifacts (credential files, shared config) are read by several modules,
and the doc that describes the artifact enumerates its consumers. When a NEW
consumer is added and that doc is not updated, the doc silently lies — and a
session that grounds on it can state a false fact about the system (origin:
2026-08-19, store_cc_token.sh's header framed the CC OAuth token as
host-Guardian-only after cc/login_health.py added a container-session consumer;
nothing detected the drift, and a later reply repeated the stale claim).

This guard is the deterministic CI backstop. For each ``yaml shared-artifact``
block in docs/architecture/shared-artifacts.md it:

  * computes the ACTUAL consumer set — every file under src/ and scripts/ that
    contains ANY of the entry's ``match_literals`` as a FIXED substring (never a
    regex — no CLI/argv grammar to re-implement), minus the ``allowlist`` and the
    ``documented_in`` writer/doc;
  * diffs that against the DECLARED ``readers`` list, both directions:
      - an actual consumer not in ``readers`` → hard ``::error::`` (exit 1);
      - a declared reader that no longer references the artifact → hard
        ``::error::`` (exit 1) — the registry lies.

Two ``match_literals`` cover the two ways code reaches an artifact: the FILENAME
literal (direct file access) and the canonical LOADER SYMBOL (transitive access
through the accessor function) — e.g. ``[cc_oauth_token.env, load_cc_oauth_token]``
so both cc/login_health.py (reads the file) and guardian/diagnosis.py (calls the
loader) are seen. Both are plain substrings, so no LSP is needed in CI.

The ``documented_in`` file must itself point readers to this registry (the guard
checks it contains the registry's filename), so the human-facing doc routes to the
CI-checked source of truth instead of maintaining a parallel prose consumer list
that can silently drift — that prose is exactly what drifted in the origin incident.

The guard fails CLOSED: a registry that parses to zero blocks, a duplicate/invalid
key, an unreadable directory under a scan root, or an unrecognised shape all block
(exit 1) rather than reporting CLEAN. The consumer scan follows symlinked dirs
(with a loop guard) so a consumer behind a symlink is not silently missed.

Scope: this guards only ENROLLED artifacts by declared-vs-actual consumer set.
It is NOT a general prose↔code drift checker (that is unbounded). A prose
freshness stamp is intentionally out of scope for now (keeps the CI job
history-free — no fetch-depth: 0). SCAN_ROOTS is fixed to src/ + scripts/: an
artifact whose consumers live outside those roots must extend SCAN_ROOTS too.

Known limitations (accepted — both inherent to fixed-substring matching, and both
FAIL-SAFE, never a wrong-green on an undeclared consumer):
  * a consumer that reaches an artifact ONLY via an alias/wrapper of the loader —
    containing neither ``match_literal`` — is not seen. Enumerate consumers when
    enrolling; if such a wrapper appears, add its symbol to ``match_literals``.
  * a literal that survives only in a COMMENT (a declared reader kept a comment
    mention after dropping the real access) reads as still-consuming, so that reader
    is not flagged stale. Distinguishing code from comments needs per-language
    parsing (an unbounded surface); the failure direction is benign — a stale reader
    is retained in the doc, never a real consumer missed.

Usage:  python scripts/check_shared_artifact_consumers.py   (exit 0 = clean, 1 = drift)
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

REGISTRY_PATH = Path("docs/architecture/shared-artifacts.md")
# Code roots scanned for consumers. Docs/tests are intentionally NOT scanned:
# prose is not a consumer, and tests reference artifacts as fixtures.
SCAN_ROOTS = ["src", "scripts"]
_SKIP_DIRS = {"__pycache__", ".git", "node_modules", ".mypy_cache", ".ruff_cache"}
# This guard is the enforcement mechanism, never a consumer — exclude it from
# every entry's scan so example literals in its own docstring don't self-flag.
# Basename-derived so it tracks a rename within scripts/.
_GUARD_SELF = f"scripts/{Path(__file__).name}"

_BLOCK_RE = re.compile(r"^```yaml[ \t]+shared-artifact[ \t]*\n(.*?)^```", re.M | re.S)


def _duplicate_top_level_keys(raw: str) -> list[str]:
    """Top-level mapping keys that appear more than once in a block.

    ``yaml.safe_load`` silently keeps the last value for a repeated key, so a
    badly-resolved merge conflict (two ``match_literals:`` lines) would silently
    narrow the literal set and blind the scan. A column-0 ``key:`` scan catches
    that realistic case without resorting to an unsafe custom yaml Loader.
    """
    seen: set[str] = set()
    dups: list[str] = []
    for line in raw.splitlines():
        m = re.match(r"([A-Za-z_][\w-]*):", line)  # column 0 → a top-level key
        if m:
            key = m.group(1)
            if key in seen:
                dups.append(key)
            seen.add(key)
    return dups


@dataclass
class Entry:
    """One ``yaml shared-artifact`` block: an artifact and its declared consumers."""

    artifact: str
    documented_in: str
    match_literals: list[str]
    readers: list[str]
    allowlist: list[str] = field(default_factory=list)


def parse_registry(text: str) -> tuple[list[Entry], list[str]]:
    """Parse every ``yaml shared-artifact`` fenced block → (entries, errors)."""
    entries: list[Entry] = []
    errors: list[str] = []
    matched = 0
    # Count fence OPENINGS: an opener with no matching close never becomes a block,
    # so if any opener is unmatched a malformed/unclosed block was silently skipped.
    open_fences = len(re.findall(r"(?m)^```yaml[ \t]+shared-artifact\b", text))
    for i, match in enumerate(_BLOCK_RE.finditer(text), start=1):
        matched += 1
        raw = match.group(1)
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            errors.append(f"block #{i}: invalid yaml ({exc})".replace("\n", " "))
            continue
        if not isinstance(data, dict):
            errors.append(f"block #{i}: expected a mapping, got {type(data).__name__}")
            continue
        dups = _duplicate_top_level_keys(raw)
        if dups:
            errors.append(
                f"block #{i}: duplicate key(s) {sorted(set(dups))} — refusing "
                f"(safe_load's last-wins would silently drop config)"
            )
            continue
        artifact = data.get("artifact")
        documented_in = data.get("documented_in")
        literals = data.get("match_literals")
        readers = data.get("readers")
        allowlist = data.get("allowlist", [])
        if not artifact or not isinstance(artifact, str):
            errors.append(f"block #{i}: missing 'artifact' name")
            continue
        if not documented_in or not isinstance(documented_in, str):
            errors.append(f"artifact '{artifact}': missing 'documented_in' path")
            continue
        if not literals or not isinstance(literals, list):
            errors.append(f"artifact '{artifact}': missing or empty 'match_literals' list")
            continue
        if not all(isinstance(x, str) for x in literals):
            errors.append(f"artifact '{artifact}': every 'match_literals' entry must be a string")
            continue
        if any(x == "" for x in literals):
            errors.append(
                f"artifact '{artifact}': 'match_literals' has an empty string (matches every file)"
            )
            continue
        if readers is None or not isinstance(readers, list):
            errors.append(f"artifact '{artifact}': missing 'readers' list (use [] if none)")
            continue
        if not isinstance(allowlist, list):
            errors.append(f"artifact '{artifact}': 'allowlist' must be a list")
            continue
        entries.append(
            Entry(
                artifact=artifact,
                documented_in=documented_in,
                match_literals=[str(x) for x in literals],
                readers=[str(x) for x in readers],
                allowlist=[str(x) for x in allowlist],
            )
        )
    if open_fences > matched:
        errors.append(
            f"{open_fences - matched} 'yaml shared-artifact' fence(s) opened but did not "
            f"close/parse (malformed or unclosed block) — refusing rather than silently skipping."
        )
    return entries, errors


def actual_consumers(
    base: Path,
    scan_roots: list[str],
    literals: list[str],
    excluded: set[str],
    on_error=None,
) -> set[str]:
    """Repo-relative paths of files under scan_roots containing ANY literal (fixed substring).

    ``excluded`` (the writer/doc + allowlist, as posix relpaths) are dropped.
    Fixed-substring match — a literal is data, never a regex. Symlinked directories
    under a scan root are NOT traversed (os.walk followlinks=False); each is instead
    reported via ``on_error`` so the caller fails CLOSED — a consumer behind a
    symlink is surfaced loudly, never silently missed, and there is no real-vs-symlink
    path-attribution ambiguity. ``on_error`` is also called when a directory cannot be
    listed; unreadable individual files are skipped.
    """
    found: set[str] = set()
    report = on_error if on_error is not None else (lambda _exc: None)
    for root in scan_roots:
        root_dir = base / root
        if not root_dir.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root_dir, onerror=report):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for d in list(dirnames):
                if (Path(dirpath) / d).is_symlink():
                    rel = (Path(dirpath) / d).relative_to(base).as_posix()
                    report(OSError(f"symlinked directory not traversed: {rel}"))
            for name in filenames:
                path = Path(dirpath) / name
                rel = path.relative_to(base).as_posix()
                if rel in excluded:
                    continue
                try:
                    content = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if any(lit in content for lit in literals):
                    found.add(rel)
    return found


def check_consumers(entry: Entry, actual: set[str]) -> tuple[set[str], set[str]]:
    """(undeclared, stale): actual-not-declared, and declared-not-actual."""
    declared = set(entry.readers)
    return actual - declared, declared - actual


def main() -> int:
    if not REGISTRY_PATH.is_file():
        print(f"::error::shared-artifact guard: {REGISTRY_PATH} not found (run from repo root)")
        return 1

    try:
        text = REGISTRY_PATH.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        print(f"::error::{REGISTRY_PATH}: could not read the registry ({exc}).")
        return 1

    entries, errors = parse_registry(text)
    for err in errors:
        print(f"::error::{REGISTRY_PATH}: {err}")
    if not entries and not errors:
        # Fail CLOSED: the registry file exists but yielded no blocks — it was
        # emptied, a fence is malformed/unclosed, or the file has CRLF endings the
        # block regex does not match. Reporting "CLEAN (0 artifacts)" would be a
        # wrong-green on the guard.
        print(
            f"::error::{REGISTRY_PATH}: no 'yaml shared-artifact' blocks parsed — the registry "
            f"is empty, a fence is malformed/unclosed, or the file has CRLF line endings. "
            f"Guard fails closed rather than passing."
        )
        return 1

    problems = 0
    base = Path.cwd()
    scan_errors: list[str] = []
    for entry in entries:
        # documented_in must ROUTE to this registry rather than maintain a parallel
        # prose consumer list that CI can't check (that prose is exactly what drifts).
        doc = Path(entry.documented_in)
        if not doc.is_file():
            print(
                f"::error::{REGISTRY_PATH}: artifact '{entry.artifact}' documented_in "
                f"'{entry.documented_in}' does not exist."
            )
            problems += 1
        elif REGISTRY_PATH.name not in doc.read_text(encoding="utf-8", errors="ignore"):
            print(
                f"::error::{REGISTRY_PATH}: artifact '{entry.artifact}' documented_in "
                f"'{entry.documented_in}' must point readers to the authoritative registry "
                f"({REGISTRY_PATH.name}) instead of only re-enumerating consumers in prose."
            )
            problems += 1

        excluded = set(entry.allowlist) | {entry.documented_in, _GUARD_SELF}
        actual = actual_consumers(
            base, SCAN_ROOTS, entry.match_literals, excluded, on_error=scan_errors.append
        )
        undeclared, stale = check_consumers(entry, actual)
        for path in sorted(undeclared):
            print(
                f"::error::{REGISTRY_PATH}: artifact '{entry.artifact}' has an UNDECLARED "
                f"consumer '{path}' — it references the artifact but is not in the entry's "
                f"'readers'. Add it, and update {entry.documented_in} if the consumer story changed."
            )
            problems += 1
        for path in sorted(stale):
            print(
                f"::error::{REGISTRY_PATH}: artifact '{entry.artifact}' lists reader '{path}', "
                f"which no longer references it (file gone or reference removed) — the registry "
                f"lies; update the entry (and {entry.documented_in})."
            )
            problems += 1

    # Fail CLOSED on any directory the scan could not read — a swallowed dir could
    # hide an undeclared consumer, which is the exact wrong-green the guard prevents.
    for exc in scan_errors:
        print(
            f"::error::{REGISTRY_PATH}: consumer scan could not read a path ({exc}) — "
            f"guard fails closed rather than reporting an incomplete-but-clean scan."
        )
        problems += 1

    if errors or problems:
        return 1
    print(f"Shared-artifact guard: CLEAN ({len(entries)} artifact(s) checked)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
