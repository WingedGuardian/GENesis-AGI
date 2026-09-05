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
# A GENUINE heading, not any line whose stripped text matches: CommonMark
# treats up to three spaces of indent as a heading and four or more as an
# indented code block, so `    ## [Unreleased]` is code — matching it via
# .strip() would fold entries after a code line, outside any real section.
# The same 0-3 rule the body validators above already encode.
_UNRELEASED_HEADING_RE = re.compile(r"^ {0,3}## \[Unreleased\]\s*$")


def _unreleased_line_index(lines: list[str]) -> int | None:
    """Index of the first GENUINE ``## [Unreleased]`` heading, or None.

    Genuine means what a CommonMark reader would render as a heading: at most
    three spaces of indent (four or more is an indented code block — the regex
    handles that) AND outside any fenced code block — the case the regex alone
    cannot see, because a fence puts an unindented copy of the heading line
    into code context. Folding after a code line lands every generated entry
    inside the fence, rendered as literal code above the real section.

    Fence tracking follows CommonMark's shape without a full parser: any
    ``_CODE_FENCE`` line opens a fence (its info string is irrelevant here); it
    closes only on a line that is nothing but the SAME character repeated at
    least as many times — an info-string line like `````python``
    inside an open fence is content, not a closer.
    """
    fence_char: str | None = None
    fence_len = 0
    for i, ln in enumerate(lines):
        if fence_char is not None:
            stripped = ln.strip()
            if stripped and set(stripped) == {fence_char} and len(stripped) >= fence_len:
                fence_char = None
            continue
        m = _CODE_FENCE.match(ln)
        if m:
            run = m.group().lstrip()
            fence_char, fence_len = run[0], len(run)
            continue
        if _UNRELEASED_HEADING_RE.match(ln):
            return i
    return None


# Block constructs that stay block constructs when spliced in. CommonMark allows
# up to THREE spaces of indent before each of these, and a list item's content
# column is set by its own bullet width — so under `-   a bullet` a two-space
# indent is still column 2 of the document and still a heading. "Indent it" is
# only a remedy at four spaces, which is what the error messages now say.
_ATX_HEADING = re.compile(r"^ {0,3}#{1,6}([ \t]|$)")
_CODE_FENCE = re.compile(r"^ {0,3}(?:`{3,}|~{3,})")
# Setext headings underline the line above with = or -, so they are headings
# without a leading '#'. `---` is also a thematic break; either way it is a
# top-level block, and the rule is the same: indent it or drop it.
_SETEXT_OR_RULE = re.compile(r"^ {0,3}(?:=+|-{2,}|(?:\*[ \t]*){3,}|(?:_[ \t]*){3,}) *$")
_HTML_BLOCK = re.compile(r"^ {0,3}<[A-Za-z!/?]")


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

    Validating only the FIRST line is not enough: a body can open with a legal
    bullet and carry a block construct three lines down, and it is spliced in
    exactly as written.

    WHAT THE DAMAGE ACTUALLY IS — stated precisely, because an earlier version
    of this docstring overstated it and the overstatement outlived the code it
    described. The assembler is NOT affected: :func:`splice` anchors on the
    ``[Unreleased]`` heading and never computes a section END, so a stray ``## ``
    does not orphan anything and a later fold still lands correctly (verified
    against the shipped code). The damage is to how the file READS: an injected
    ``## `` produces a phantom release that a human — or any tool that splits on
    ``^## ``, which is what release step 6 does by hand — will mistake for a real
    one. An unclosed fence is the worst of them, because every entry below it
    renders as code on GitHub.

    So this is a rendering and release-notes gate, not a data-integrity one. It
    is still worth having: the failure is invisible in the assembler's output and
    surfaces at publish time.

    The checks are written as CLASSES, not as the spellings that were reported.
    ``## `` was the reported case, but every ATX level, both fence characters,
    setext underlines and thematic breaks are the same construct, and CommonMark
    allows all of them up to three spaces of indent. Enumerating the reported
    spelling is how a class ships in pieces — which happened here twice.
    """
    if not body.startswith("- "):
        raise FragmentError(
            f"{name}: fragment must start with a Markdown bullet ('- '), because "
            "it is spliced verbatim under a category heading. Found: "
            f"{body.splitlines()[0][:60]!r}"
        )
    for i, line in enumerate(body.splitlines(), start=1):
        if _ATX_HEADING.match(line) or _SETEXT_OR_RULE.match(line):
            raise FragmentError(
                f"{name}: line {i} is a top-level Markdown heading or rule "
                f"({line[:40]!r}). A fragment is spliced verbatim into the "
                "[Unreleased] section, so this reads as a new release heading "
                "there — to a person, and to anything that splits the file on "
                "'## '. Indent it FOUR spaces to keep it inside the bullet "
                "(two is not enough under a wider bullet, and CommonMark treats "
                "up to three spaces as unindented)."
            )
        if _CODE_FENCE.match(line):
            raise FragmentError(
                f"{name}: line {i} opens a top-level code fence ({line[:40]!r}). "
                "Unclosed, it renders every entry below it as code. Indent it "
                "FOUR spaces so it stays inside the bullet."
            )
        if _HTML_BLOCK.match(line):
            raise FragmentError(
                f"{name}: line {i} opens a top-level HTML block ({line[:40]!r}), "
                "which ends the surrounding Markdown block when spliced. Indent "
                "it FOUR spaces, or write it as prose."
            )


def collect_fragments(directory: Path | None = None) -> list[tuple[str, str, Path, str]]:
    """Every fragment in the directory as (timestamp, Category, path, raw text).

    The raw text is captured HERE, once: rendering, the pre-delete comparison
    and the rollback all use this capture, so the bytes validated, the bytes
    folded and the bytes removed are one artifact — a file that changes after
    this read is detected rather than silently overwritten or deleted.

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
    out: list[tuple[str, str, Path, str]] = []
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
        # A fragment must be a REGULAR file. A symlink with a valid fragment
        # name publishes another file's body under this name (a `security`
        # link to a `fixed` fragment ships one body under two categories), and
        # the file deleted at fold time is not the artifact whose content was
        # validated. Checked before the name exemption so a symlinked
        # README.md errors too instead of being exempted.
        if path.is_symlink():
            raise FragmentError(
                f"{path.name}: is a symlink; a fragment must be a regular file "
                "so the validated body and the file removed at fold time are "
                "one artifact"
            )
        # And a regular file it must actually BE — is_symlink alone under-
        # enforces the invariant: a FIFO with a valid fragment name would hang
        # the read forever, a socket corrupts it. Classified before any read.
        if not path.is_file():
            raise FragmentError(
                f"{path.name}: not a regular file; a fragment must be a plain "
                "file so it can be read, folded and removed as one artifact"
            )
        if path.name in NON_FRAGMENTS:
            continue
        # Editor and VCS debris (.DS_Store, .gitkeep, .foo.md.swp). Refusing to
        # run a RELEASE because of an untracked local file git never sees would
        # split the two surfaces: CI on a clean checkout stays green while the
        # maintainer's own run fails on their own debris.
        #
        # But the exemption stops at Markdown. A committed `.20260904…-fixed-x.md`
        # is a would-be fragment, and skipping it in a clean CI checkout would
        # report success while release assembly omitted its entry — the exact
        # silent omission this directory's classification exists to prevent. So
        # a dot-prefixed *.md is classified like any other .md and errors.
        if path.name.startswith(".") and path.suffix != ".md":
            continue
        ts, category, _slug = parse_fragment_name(path.name)
        try:
            raw = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # The contract is a single 'changelog.d: <message>' line and exit 2.
            # An unreadable or unreadably-encoded fragment must not surface as
            # a traceback that a CI job cannot classify.
            raise FragmentError(f"{path.name}: cannot read fragment: {exc}") from exc
        body = raw.strip()
        if not body:
            raise FragmentError(f"{path.name}: fragment is empty")
        _validate_body(path.name, body)
        out.append((ts, category, path, raw))
    # No duplicate-name check here on purpose: a directory cannot hold two files
    # with one name, so such a check could never fire. Two branches choosing the
    # same name collide as a git add/add conflict, which git reports on its own —
    # the mitigation is the timestamp (and not copying the example name), not a
    # validator that would only look like protection.
    return sorted(out, key=lambda r: (CATEGORIES.index(r[1]), r[0]))


def render_sections(fragments: list[tuple[str, str, Path, str]]) -> dict[str, list[str]]:
    """Category -> the fragment bodies belonging to it, in timestamp order.

    Rendered from the text captured at validation time, never a re-read: what
    was validated is what is folded, whatever happens to the file in between.
    """
    sections: dict[str, list[str]] = {}
    for _ts, category, _path, raw in fragments:
        sections.setdefault(category, []).append(raw.strip())
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
    start = _unreleased_line_index(lines)
    if start is None:
        raise FragmentError(f"CHANGELOG.md has no '{UNRELEASED_HEADING}' heading to fold into")

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
    if _unreleased_line_index(text.splitlines()) is None:
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
    # A fold is not "CHANGELOG.md was touched" — that test is satisfied by a
    # typo fix or a stray newline, which would disarm this rule for the whole
    # change. The question is whether each dropped fragment's entry ARRIVED in
    # the changelog, so compare against the lines the diff actually ADDED.
    diffed = subprocess.run(
        ["git", "diff", "-U0", base, "--", CHANGELOG.name],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO_ROOT,
    )
    # Exact LINES the diff added, not one glued string. A substring test over a
    # glued blob is satisfiable by accident — a short entry like "- fix" occurs
    # inside unrelated added text — and checking only the FIRST line lets a
    # partial manual fold that dropped the continuation paragraphs pass. The
    # fold claim is only proven when EVERY nonblank line of the deleted
    # fragment arrived, as lines.
    added_lines = (
        {
            ln[1:].rstrip()
            for ln in diffed.stdout.splitlines()
            if ln.startswith("+") and not ln.startswith("+++")
        }
        if diffed.returncode == 0
        else set()
    )
    still_lost: list[str] = []
    for name in lost:
        blob = subprocess.run(
            ["git", "show", f"{base}:{FRAGMENT_DIR.name}/{name}"],
            capture_output=True,
            text=True,
            check=False,
            cwd=REPO_ROOT,
        )
        body_lines = [ln.rstrip() for ln in blob.stdout.splitlines() if ln.strip()]
        # Fail CLOSED on an unreadable blob: not being able to tell is not the
        # same as being satisfied.
        if (
            blob.returncode != 0
            or not body_lines
            or any(ln not in added_lines for ln in body_lines)
        ):
            still_lost.append(name)
    if not still_lost:
        return  # every dropped fragment's entry is now IN the changelog
    raise FragmentError(
        f"{len(still_lost)} fragment(s) present at {base} are gone and their "
        f"entries are not in CHANGELOG.md: {', '.join(still_lost[:5])}. Before "
        "assembly a fragment is the only copy of its entry, so this drops it "
        "from the next release silently. If this IS the release fold, the "
        "entries must appear in CHANGELOG.md — touching the file is not enough."
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
    if args.base and not args.check:
        # Reaching for the guard flag must never perform the irreversible
        # fold. Argparse accepts the combination without comment.
        parser.error("--base is only meaningful with --check")

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
        result = splice(CHANGELOG.read_text(encoding="utf-8"), render_sections(fragments))
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
            # Written as UTF-8 BYTES rather than through the text layer, whose
            # encoding follows the ambient locale: on an 8-bit locale `print`
            # raises UnicodeEncodeError on any accented character, so --check
            # would certify green while --dry-run died on the same content.
            sys.stdout.buffer.write(result.encode("utf-8"))
            sys.stdout.buffer.flush()
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
    original = CHANGELOG.read_text(encoding="utf-8")
    tmp = CHANGELOG.with_name(CHANGELOG.name + ".tmp")
    tmp.write_text(result, encoding="utf-8")
    try:
        tmp.replace(CHANGELOG)
    except OSError:
        tmp.unlink(missing_ok=True)  # never leave a half-finished twin behind
        raise
    # Cleanup is part of the same operation, not a tidy-up after it. If a
    # fragment cannot be removed — an unwritable directory, a partial loop —
    # the changelog is rolled back to what it was, because the alternative is a
    # half-done state whose obvious remedy makes it worse: re-running the
    # documented command would fold the surviving fragments in a SECOND time and
    # duplicate those entries in the release notes. All-or-nothing keeps a rerun
    # correct.
    removed: list[tuple[Path, str]] = []
    try:
        for _ts, _category, path, raw in fragments:
            # The bytes about to be deleted must be the bytes that were folded.
            # An editor save landing between the assembly read and this loop
            # would otherwise be destroyed silently: CHANGELOG.md carries the
            # older text, the newer bytes exist nowhere afterwards, and git
            # never saw them. Refuse and roll back; the changed file itself is
            # left untouched, still carrying the newer content.
            if path.read_text(encoding="utf-8") != raw:
                raise FragmentError(
                    f"{path.name}: changed on disk after it was read for "
                    "assembly; refusing to delete the newer content"
                )
            # Capture BEFORE unlink, so there is no instant at which a file is
            # gone but unrecorded. The rollback rewriting a file that was never
            # actually unlinked is harmless — same content, idempotent.
            removed.append((path, raw))
            path.unlink()
    except BaseException as exc:
        # Put back everything already destroyed, not just the changelog. Rolling
        # back only CHANGELOG.md would leave the fragments deleted BEFORE the
        # failure in neither place — the exact silent loss this directory exists
        # to prevent, reintroduced by its own recovery path, and the message
        # would then send the operator to re-run and ship without them.
        #
        # BaseException on purpose: Ctrl-C lands here too, and an interrupted
        # fold left half-done invites the re-run that duplicates entries. This
        # cannot cover SIGKILL or a power cut — no in-process handler can — and
        # the durable recovery for THOSE is git itself: fragments and
        # CHANGELOG.md are tracked, so `git status` shows the half-done state
        # and `git restore` returns to the pre-fold tree. That, not a staging
        # scheme, is the recovery of record; building a second one beside git
        # would be a store nobody reconciles.
        for done, text in removed:
            done.write_text(text, encoding="utf-8")
        CHANGELOG.write_text(original, encoding="utf-8")
        print(
            f"changelog.d: interrupted while removing fragments ({exc!r}); every "
            "fragment and CHANGELOG.md have been restored, so nothing was lost "
            "and a rerun is correct. (After a hard kill, recover with git: the "
            "fragments and CHANGELOG.md are tracked files.)",
            file=sys.stderr,
        )
        if not isinstance(exc, (OSError, FragmentError)):
            raise  # Ctrl-C etc. must still terminate as what they are
        return 2
    print(
        f"changelog.d: folded {len(fragments)} fragment(s) into CHANGELOG.md "
        "and removed them. Review the [Unreleased] section before tagging."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
