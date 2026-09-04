#!/usr/bin/env python3
"""Fold ``changelog.d/`` fragments into ``CHANGELOG.md``'s [Unreleased] section.

WHY THIS EXISTS. Two branches that each add an entry to a shared changelog are
not disagreeing about anything — they are inserting at the same position, which
git reports as a conflict a human resolves by hand. Measured 2026-09-04 against
this repository's own queue: of 49 open pull requests, 21 could not merge, and
18 of those 21 conflicted on ``CHANGELOG.md`` and nothing else.

A fragment per change removes the collision instead of resolving it: two
branches write two different filenames, so there is nothing to merge. GitHub
never computes a conflict, which matters because GitHub ignores a repository's
``.gitattributes`` server-side and so cannot be taught to merge a shared file
more cleverly.

USAGE
    scripts/assemble_changelog.py --check     # validate fragments (CI)
    scripts/assemble_changelog.py --dry-run   # print what would be written
    scripts/assemble_changelog.py             # fold in, then delete fragments

Run the assembly at RELEASE time, not per pull request — folding early
re-creates the shared file everyone edits, which is the thing this avoids.
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FRAGMENT_DIR = REPO_ROOT / "changelog.d"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

# Keep a Changelog's categories, in the order they are rendered. The order is
# part of the output contract, not an implementation detail — a release section
# should read the same way every time.
CATEGORIES = ("Added", "Changed", "Deprecated", "Removed", "Fixed", "Security")
_CATEGORY_BY_KEY = {c.lower(): c for c in CATEGORIES}

# <14-digit UTC timestamp>-<category>-<slug>.md
FRAGMENT_PATTERN = re.compile(
    r"^(?P<ts>[0-9]{14})-(?P<category>[a-z]+)-(?P<slug>[a-z0-9]+(?:-[a-z0-9]+)*)\.md$"
)

# Files that live in the directory but are not fragments. Anything NOT matching
# a fragment and NOT named here is a violation rather than something to skip —
# a silently ignored file is a changelog entry that never ships.
NON_FRAGMENTS = frozenset({"README.md"})

UNRELEASED_HEADING = "## [Unreleased]"


class FragmentError(Exception):
    """A fragment (or the directory) violates the contract."""


def _valid_timestamp(ts: str) -> bool:
    """Whether a 14-digit id is a real UTC calendar time, not just digits.

    ``20261301000000`` is fourteen digits and is not a date. Validating it here
    means a typo is caught at ``--check`` time rather than producing an entry
    that sorts into the wrong place forever.
    """
    try:
        dt.datetime.strptime(ts, "%Y%m%d%H%M%S")
    except ValueError:
        return False
    return True


def parse_fragment_name(name: str) -> tuple[str, str, str]:
    """(timestamp, Category, slug) for a fragment filename, or raise."""
    m = FRAGMENT_PATTERN.match(name)
    if not m:
        raise FragmentError(
            f"{name}: not a valid fragment name. Expected "
            "<YYYYMMDDHHMMSS>-<category>-<slug>.md, e.g. "
            "20260904210000-fixed-changelog-collisions.md "
            f"(categories: {', '.join(sorted(_CATEGORY_BY_KEY))})"
        )
    ts, key, slug = m.group("ts"), m.group("category"), m.group("slug")
    if not _valid_timestamp(ts):
        raise FragmentError(f"{name}: '{ts}' is not a real UTC timestamp")
    if key not in _CATEGORY_BY_KEY:
        raise FragmentError(
            f"{name}: unknown category '{key}'. Use one of: {', '.join(sorted(_CATEGORY_BY_KEY))}"
        )
    return ts, _CATEGORY_BY_KEY[key], slug


def _validate_body(name: str, body: str) -> None:
    """Reject a body that would restructure CHANGELOG.md when spliced verbatim.

    Validating only the FIRST line is not enough, and the failure is permanent
    rather than cosmetic. A body whose first line is a legal bullet but which
    contains a ``## `` heading further down injects that heading INTO the
    [Unreleased] section; every later assembly then treats it as the section's
    end, orphaning everything below it into a phantom release and starting a
    duplicate category group above. Exit 0, no warning, and each subsequent run
    inherits the damage. An unindented code fence does the same thing by
    swallowing the structure between its markers.

    Both are avoidable by indenting: a fence indented two spaces stays inside
    the bullet, which is where it belongs anyway.
    """
    if not body.startswith("- "):
        raise FragmentError(
            f"{name}: fragment must start with a Markdown bullet ('- '), because "
            "it is spliced verbatim under a category heading. Found: "
            f"{body.splitlines()[0][:60]!r}"
        )
    for i, line in enumerate(body.splitlines(), start=1):
        if line.startswith("## "):
            raise FragmentError(
                f"{name}: line {i} starts a Markdown section heading "
                f"({line[:40]!r}). A fragment is spliced verbatim INSIDE the "
                "[Unreleased] section, so this heading would end that section "
                "early and orphan everything below it on every future assembly."
            )
        if line.startswith("```"):
            raise FragmentError(
                f"{name}: line {i} opens a top-level code fence ({line[:40]!r}). "
                "Indent it two spaces so it stays inside the bullet — at column "
                "zero it swallows the surrounding structure when spliced."
            )


def collect_fragments(directory: Path | None = None) -> list[tuple[str, str, Path]]:
    """Every fragment in the directory as (timestamp, Category, path).

    Classifies EVERY entry: a file that is neither a fragment nor an explicitly
    known non-fragment raises. A directory raises for the same reason — the
    convention is flat, and a nested file would be silently skipped by a glob.

    ``directory`` defaults to :data:`FRAGMENT_DIR`, resolved on every CALL rather
    than captured as a default argument. A default argument would bind the path
    at import time, so redirecting the module global would silently have no
    effect and the function would keep operating on the real directory — which
    it then DELETES from. That is a seam that looks configurable and is not.
    """
    directory = FRAGMENT_DIR if directory is None else directory
    if not directory.is_dir():
        return []
    out: list[tuple[str, str, Path]] = []
    for path in sorted(directory.iterdir()):
        # The flat-directory rule is checked FIRST, before any name-based
        # exemption. Exempting by name first would let a DIRECTORY named
        # `README.md` be skipped wholesale, hiding every fragment inside it
        # while --check reported success — the name exemption would have
        # punched a hole straight through the total-classification guarantee.
        if path.is_dir():
            raise FragmentError(
                f"{path.name}/: changelog.d is flat; a nested directory would be "
                "skipped by the assembler and its entries would never ship"
            )
        if path.name in NON_FRAGMENTS:
            continue
        # Editor and VCS debris (.DS_Store, .gitkeep, vim swapfiles). A fragment
        # name always starts with a digit, so nothing that starts with a dot can
        # be one — and refusing to run a RELEASE because of an untracked local
        # file git never sees would also split the two surfaces: CI runs on a
        # clean checkout and would stay green while the maintainer's own run
        # failed. The "never silently skipped" property lives on *.md files.
        if path.name.startswith("."):
            continue
        ts, category, _slug = parse_fragment_name(path.name)
        try:
            body = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError) as exc:
            # The contract is a single 'changelog.d: <message>' line and exit 2.
            # An unreadable, unreadably-encoded or dangling-symlink fragment must
            # not surface as a traceback that a CI job cannot classify.
            raise FragmentError(f"{path.name}: cannot read fragment: {exc}") from exc
        if not body:
            raise FragmentError(f"{path.name}: fragment is empty")
        _validate_body(path.name, body)
        out.append((ts, category, path))
    # No duplicate-name check here on purpose: a directory cannot hold two files
    # with one name, so such a check could never fire. Two branches choosing the
    # same name collide as a git add/add conflict, which git reports on its own —
    # the mitigation is the timestamp (and not copying the example name), not a
    # validator that would only look like protection.
    return sorted(out, key=lambda r: (CATEGORIES.index(r[1]), r[0]))


def render_sections(fragments: list[tuple[str, str, Path]]) -> dict[str, list[str]]:
    """Category -> the fragment bodies belonging to it, in timestamp order."""
    sections: dict[str, list[str]] = {}
    for _ts, category, path in fragments:
        sections.setdefault(category, []).append(path.read_text().strip())
    return sections


def splice(changelog_text: str, sections: dict[str, list[str]]) -> str:
    """Insert the fragments at the TOP of the [Unreleased] block.

    Placement is deliberately "top of the section", not "under the existing
    heading for this category", and the real file is why. MEASURED 2026-09-04 on
    this repository's CHANGELOG.md: [Unreleased] spans 3,203 lines and holds 40
    category headings — 14 ``### Fixed``, 13 ``### Added``, 9 ``### Changed``,
    3 ``### Security``, 1 ``### Removed`` — because entries have been appended in
    batches, each bringing its own headings. Reusing the first matching heading
    put a new Security entry 810 lines into the section and a new Removed entry
    2,267 lines in, below unrelated older content, where nobody cutting a release
    would see it. Inserting at the top makes placement independent of whatever
    batch structure the file has accumulated: a new entry is always within a few
    lines of the [Unreleased] heading.

    Categories are emitted in Keep a Changelog order so a release section reads
    the same way every time. Existing content is never moved or reordered.
    """
    lines = changelog_text.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.strip() == UNRELEASED_HEADING)
    except StopIteration:
        raise FragmentError(
            f"CHANGELOG.md has no '{UNRELEASED_HEADING}' heading to fold into"
        ) from None

    rendered: list[str] = []
    for category in CATEGORIES:
        entries = sections.get(category)
        if not entries:
            continue
        rendered.extend(["", f"### {category}", ""])
        for body in entries:
            rendered.extend(body.splitlines())
            rendered.append("")

    if not rendered:
        return changelog_text if changelog_text.endswith("\n") else changelog_text + "\n"

    # Immediately after the heading, skipping the blank line that follows it, so
    # the new groups sit at the very top of the section.
    at = start + 1
    while at < len(lines) and not lines[at].strip():
        at += 1
    # `rendered` opens with a blank line, so drop the one we just skipped past
    # rather than emitting two.
    lines[at:at] = rendered[1:] + [""]
    return "\n".join(lines) + "\n"


def _check_target() -> None:
    """The destination has to be usable, or --check certifies half the contract.

    Validating only the fragments leaves the job green while a PR deletes
    CHANGELOG.md or renames its ``[Unreleased]`` heading; the mismatch then
    surfaces at the first release, to whoever is cutting it rather than to
    whoever caused it.
    """
    try:
        text = CHANGELOG.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise FragmentError(f"cannot read {CHANGELOG.name}: {exc}") from exc
    if not any(ln.strip() == UNRELEASED_HEADING for ln in text.splitlines()):
        raise FragmentError(
            f"{CHANGELOG.name} has no '{UNRELEASED_HEADING}' heading — fragments "
            "would have nowhere to be folded into at release"
        )


def _check_no_fragment_lost(base: str) -> None:
    """Refuse a change that drops a fragment the base branch already had.

    Before assembly a fragment is the ONLY copy of its entry, so deleting one
    removes it from the next release with nothing to recover it from, and every
    remaining file still validates — the job would pass. The one legitimate
    deletion is the release fold, which removes every fragment and rewrites
    CHANGELOG.md in the same change; that pairing is the signal, so no override
    token is needed for it.
    """
    listed = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", "-z", base, "--", FRAGMENT_DIR.name],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    if listed.returncode != 0:
        # Fail CLOSED: an unreadable base means the rule checked nothing, and a
        # silent pass here is indistinguishable from a clean result.
        raise FragmentError(
            f"cannot read fragments at base {base!r}: {listed.stderr.strip()} "
            "(a shallow clone has no base to compare against)"
        )
    was = {
        name.rsplit("/", 1)[-1]
        for name in listed.stdout.split("\0")
        if name.endswith(".md") and name.rsplit("/", 1)[-1] not in NON_FRAGMENTS
    }
    now = {p.name for p in FRAGMENT_DIR.iterdir()} if FRAGMENT_DIR.is_dir() else set()
    lost = sorted(was - now)
    if not lost:
        return
    changed = subprocess.run(
        ["git", "diff", "--name-only", base, "--", CHANGELOG.name],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    if changed.returncode == 0 and changed.stdout.strip():
        return  # the release fold: fragments consumed INTO the changelog
    raise FragmentError(
        f"{len(lost)} fragment(s) present at {base} are gone and CHANGELOG.md is "
        f"unchanged: {', '.join(lost[:5])}. Before assembly a fragment is the only "
        "copy of its entry, so this drops it from the next release silently. If "
        "this IS the release fold, it must also rewrite CHANGELOG.md."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate every fragment and exit; write nothing",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the resulting CHANGELOG.md instead of writing it",
    )
    parser.add_argument(
        "--base",
        default="",
        metavar="REF",
        help=(
            "with --check: also refuse a fragment that this ref has and the "
            "working tree does not, unless CHANGELOG.md changed too (the "
            "release fold). Fragments are the only copy of an entry."
        ),
    )
    args = parser.parse_args(argv)

    try:
        fragments = collect_fragments()
    except FragmentError as exc:
        print(f"changelog.d: {exc}", file=sys.stderr)
        return 2

    if args.check:
        # Validate the TARGET too, not only the inputs. Without this the job is
        # green while a PR deletes CHANGELOG.md or renames the [Unreleased]
        # heading, and the incompatibility surfaces at the first release —
        # from whoever is cutting it, not from whoever caused it.
        try:
            _check_target()
            if args.base:
                _check_no_fragment_lost(args.base)
        except FragmentError as exc:
            print(f"changelog.d: {exc}", file=sys.stderr)
            return 2
        print(f"changelog.d: {len(fragments)} fragment(s), all valid")
        return 0

    if not fragments:
        print("changelog.d: no fragments to assemble; CHANGELOG.md unchanged")
        return 0

    try:
        result = splice(CHANGELOG.read_text(), render_sections(fragments))
    except FragmentError as exc:
        print(f"changelog.d: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        # `--dry-run | head` is the obvious way to use this and would otherwise
        # end in a BrokenPipeError traceback: the reader closes the pipe while
        # there is still output to write. Exit quietly instead, and detach
        # stdout first so the interpreter does not try to flush it again at
        # shutdown and print a second error on the way out.
        try:
            print(result, end="")
        except BrokenPipeError:
            devnull = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull, sys.stdout.fileno())
            finally:
                os.close(devnull)
            return 0
        return 0

    # Write first, and atomically — the fragments are the only copy of these
    # entries, so a write that fails half-way must not be followed by deleting
    # them. `replace` is atomic on the same filesystem, so CHANGELOG.md is
    # either the old file or the complete new one, never a truncated middle.
    tmp = CHANGELOG.with_name(CHANGELOG.name + ".tmp")
    tmp.write_text(result)
    try:
        tmp.replace(CHANGELOG)
    except OSError:
        tmp.unlink(missing_ok=True)  # never leave a half-finished twin behind
        raise
    for _ts, _category, path in fragments:
        path.unlink()
    print(
        f"changelog.d: folded {len(fragments)} fragment(s) into CHANGELOG.md "
        "and removed them. Review the [Unreleased] section before tagging."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
