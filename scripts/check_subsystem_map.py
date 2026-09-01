#!/usr/bin/env python3
"""Subsystem-map drift guard — docs/architecture/CURRENT.md must track src/genesis.

CURRENT.md is the canonical judgment-layer subsystem map (what each subsystem
is FOR, its easy-to-forget mechanisms, its do-not-touch edges). Audits and
capability comparisons consult it FIRST — so a map that silently drifts from
the tree is worse than no map. This guard is the CI backstop:

  * Every top-level package / loose module under ``src/genesis`` must be
    claimed by exactly ONE entry's fenced ``yaml subsystem-map`` block (or
    carry a reasoned ALLOWLIST entry) → a NEW package that nobody mapped is
    a hard ``::error::`` (exit 1).
  * A claimed module that no longer exists on disk means the map lies →
    hard ``::error::`` (exit 1).
  * An entry whose modules have accumulated more than STALE_COMMIT_THRESHOLD
    commits since its ``verified: <sha> <date>`` stamp gets a ``::warning::``
    only (exit 0) — staleness nudges, it never blocks.

The staleness check needs real history: on a shallow clone (or when the
stamped sha is absent locally) it degrades gracefully — it says so and skips,
it does not guess. The CI job for this script therefore checks out with
``fetch-depth: 0`` (see .github/workflows/ci.yml, subsystem-map-check).

Map block format (one per entry in CURRENT.md; the info-string tag is what
distinguishes map blocks from ordinary yaml examples):

    ```yaml subsystem-map
    entry: memory
    modules: [memory, qdrant]
    verified: 9037d45b 2026-07-07
    ```

Usage:  python scripts/check_subsystem_map.py   (exit 0 = clean, 1 = drift)
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

MAP_PATH = Path("docs/architecture/CURRENT.md")
SRC_ROOT = Path("src/genesis")

# Top-level modules deliberately NOT mapped. Every entry MUST carry a reason;
# prefer assigning a module to a map entry over growing this list.
ALLOWLIST: dict[str, str] = {}

# Commits touching an entry's modules since its stamp before we nudge.
STALE_COMMIT_THRESHOLD = 20

_BLOCK_RE = re.compile(r"^```yaml[ \t]+subsystem-map[ \t]*\n(.*?)^```", re.M | re.S)
_VERIFIED_RE = re.compile(r"^(?P<sha>[0-9a-f]{7,40})\s+(?P<date>\d{4}-\d{2}-\d{2})$")
_SKIP_TOP_LEVEL = {"__init__.py", "__main__.py"}


@dataclass
class Entry:
    """Parsed representation of one ``yaml subsystem-map`` block in CURRENT.md."""

    name: str
    modules: list[str]
    verified_sha: str
    verified_date: str


@dataclass
class Coverage:
    """Result of diffing claimed modules against the live src/genesis tree."""

    unmapped: set[str] = field(default_factory=set)  # live, claimed by no entry
    vanished: set[str] = field(default_factory=set)  # claimed, gone from disk
    duplicates: set[str] = field(default_factory=set)  # claimed by 2+ entries
    unused_allowlist: set[str] = field(default_factory=set)  # allowlisted, no longer needed


def parse_map(text: str) -> tuple[list[Entry], list[str]]:
    """Parse every ``yaml subsystem-map`` fenced block → (entries, errors)."""
    entries: list[Entry] = []
    errors: list[str] = []
    for i, match in enumerate(_BLOCK_RE.finditer(text), start=1):
        raw = match.group(1)
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError as exc:
            errors.append(f"block #{i}: invalid yaml ({exc})".replace("\n", " "))
            continue
        if not isinstance(data, dict):
            errors.append(f"block #{i}: expected a mapping, got {type(data).__name__}")
            continue
        name = data.get("entry")
        modules = data.get("modules")
        verified = data.get("verified")
        if not name or not isinstance(name, str):
            errors.append(f"block #{i}: missing 'entry' name")
            continue
        if not modules or not isinstance(modules, list):
            errors.append(f"block '{name}': missing or empty 'modules' list")
            continue
        stamp = _VERIFIED_RE.match(str(verified).strip()) if verified else None
        if stamp is None:
            errors.append(
                f"block '{name}': 'verified' must be '<short-sha> <YYYY-MM-DD>', got {verified!r}"
            )
            continue
        entries.append(
            Entry(
                name=name,
                modules=[str(m) for m in modules],
                verified_sha=stamp.group("sha"),
                verified_date=stamp.group("date"),
            )
        )
    return entries, errors


def live_modules(src_root: Path) -> set[str]:
    """Top-level dirs (packaged or not — e.g. skills/ has no __init__.py) + loose .py modules."""
    live: set[str] = set()
    for child in src_root.iterdir():
        is_subsystem_dir = (
            child.is_dir()
            and child.name != "__pycache__"
            and not child.name.startswith(".")
        )
        is_loose_module = child.suffix == ".py" and child.name not in _SKIP_TOP_LEVEL
        if is_subsystem_dir or is_loose_module:
            live.add(child.name)
    return live


def check_coverage(
    entries: list[Entry], live: set[str], allowlist: dict[str, str]
) -> Coverage:
    """Diff claimed vs live modules both directions.

    An allowlist entry is "unused" when its module is no longer a live-but-
    unclaimed straggler (it vanished from disk, or an entry now claims it).
    """
    cov = Coverage()
    claimed: set[str] = set()
    for entry in entries:
        for mod in entry.modules:
            if mod in claimed:
                cov.duplicates.add(mod)
            claimed.add(mod)
    cov.unmapped = live - claimed - set(allowlist)
    cov.vanished = claimed - live
    cov.unused_allowlist = set(allowlist) - (live - claimed)
    return cov


def _git(args: list[str]) -> str | None:
    """Run git, return stripped stdout, or None on any failure."""
    try:
        proc = subprocess.run(
            ["git", *args], capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        # incl. TimeoutExpired — staleness is warning-only; a hung git call
        # must degrade to a skip, never crash the guard into a hard failure
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def check_staleness(entries: list[Entry], threshold: int) -> list[str] | None:
    """Per-entry commits-since-stamp warnings; None = history unavailable (skip)."""
    if _git(["rev-parse", "--is-shallow-repository"]) != "false":
        return None  # genuinely cannot count anything — the ONLY global skip

    # Resolved once, and only used when it resolves: with BOTH the stamp and this
    # ref known to exist, a failing `merge-base --is-ancestor` can only mean "not
    # an ancestor" rather than "something was missing".
    default_ref = next(
        (
            ref
            for ref in ("origin/main", "origin/master")
            if _git(["rev-parse", "--verify", f"{ref}^{{commit}}"]) is not None
        ),
        None,
    )

    warnings: list[str] = []
    for entry in entries:
        paths = [f"src/genesis/{m}" for m in entry.modules]
        # An unresolvable stamp is ONE entry's problem, reported and stepped
        # over. It used to `return None`, which abandoned the pass for EVERY
        # entry — so two orphan shas (branch commits discarded by squash-merge)
        # silenced staleness for the whole file while the run still printed
        # CLEAN. A checker that reports success while checking nothing is the
        # exact failure this file's own guard exists to catch.
        if _git(["cat-file", "-e", f"{entry.verified_sha}^{{commit}}"]) is None:
            warnings.append(
                f"entry '{entry.name}': verified stamp {entry.verified_sha} is not a commit in "
                "this history, so its staleness CANNOT be counted — re-verify the entry and "
                "stamp it with a commit that survives merge (the default-branch tip you "
                "verified against, never a feature-branch HEAD)"
            )
            continue

        # The half above is a POST-MORTEM: by the time a stamp is unresolvable
        # the damage is done. This is the pre-mortem, and it is the one that can
        # still be acted on — a stamp that exists HERE but is not an ancestor of
        # the default branch is a feature-branch commit. It resolves perfectly
        # while the PR is open and dies the instant that PR is squash-merged,
        # becoming the unresolvable stamp above.
        #
        # MEASURED 2026-08-31: a live PR carried exactly this shape and would
        # have minted the third orphan within hours. The reason a prose rule is
        # not enough: an adversarial reviewer RECOMMENDED that sha, correctly
        # reasoning about ancestry at review time while nobody was reasoning
        # about what survives the merge. A rule a careful reader can be argued
        # out of needs a mechanical check, and this is it.
        if default_ref is not None and (
            _git(["merge-base", "--is-ancestor", entry.verified_sha, default_ref]) is None
        ):
            warnings.append(
                f"entry '{entry.name}': verified stamp {entry.verified_sha} is not an ancestor "
                f"of {default_ref} — a feature-branch commit, which squash-merge DISCARDS, so "
                "this stamp goes unresolvable the moment it lands and takes the staleness "
                f"signal down with it. Re-stamp with the {default_ref} commit you verified "
                "against."
            )
            continue
        count_out = _git(["rev-list", "--count", f"{entry.verified_sha}..HEAD", "--", *paths])
        if count_out is None:
            warnings.append(
                f"entry '{entry.name}': could not count commits since {entry.verified_sha} — "
                "staleness unknown for this entry"
            )
            continue
        if int(count_out) > threshold:
            warnings.append(
                f"entry '{entry.name}': {count_out} commits touched its modules since "
                f"verified stamp {entry.verified_sha} ({entry.verified_date}) — "
                f"re-verify the entry and bump its stamp"
            )
    return warnings


def main() -> int:
    if not MAP_PATH.is_file():
        print(f"::error::subsystem-map guard: {MAP_PATH} not found (run from repo root)")
        return 1
    if not SRC_ROOT.is_dir():
        print(f"::error::subsystem-map guard: scan root {SRC_ROOT} not found")
        return 1

    entries, errors = parse_map(MAP_PATH.read_text(encoding="utf-8"))
    cov = check_coverage(entries, live_modules(SRC_ROOT), ALLOWLIST)

    for err in errors:
        print(f"::error::{MAP_PATH}: {err}")
    for mod in sorted(cov.unmapped):
        print(
            f"::error::src/genesis/{mod} is not claimed by any {MAP_PATH} entry. "
            f"Add it to the right entry's modules block (or a reasoned ALLOWLIST entry)."
        )
    for mod in sorted(cov.vanished):
        print(
            f"::error::{MAP_PATH} claims src/genesis/{mod}, which no longer exists — "
            f"the map lies; update the owning entry."
        )
    for mod in sorted(cov.duplicates):
        print(f"::error::{MAP_PATH}: module '{mod}' is claimed by more than one entry.")

    for mod in sorted(cov.unused_allowlist):
        print(f"::warning::subsystem-map ALLOWLIST entry '{mod}' no longer needed — remove it.")

    stale = check_staleness(entries, STALE_COMMIT_THRESHOLD)
    if stale is None:
        print(
            "subsystem-map guard: staleness check SKIPPED — this is a SHALLOW clone, "
            "so no entry can be counted (CI needs fetch-depth: 0). An unresolvable "
            "stamp no longer skips the pass; it is reported per entry."
        )
    else:
        for warning in stale:
            print(f"::warning::{warning}")

    if errors or cov.unmapped or cov.vanished or cov.duplicates:
        return 1
    # CLEAN is a verdict about COVERAGE only. Say so when staleness could not be
    # checked or did have something to say, so the summary line cannot be read
    # as "everything here was verified" — the reading that let a silently
    # skipped staleness pass sit under a green line for weeks.
    if stale is None:
        staleness = " — staleness NOT checked (shallow clone)"
    elif stale:
        staleness = f" — but {len(stale)} staleness warning(s) above"
    else:
        staleness = ""
    print(
        f"Subsystem-map guard: coverage CLEAN ({len(entries)} entries cover "
        f"{len(live_modules(SRC_ROOT))} top-level modules){staleness}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
