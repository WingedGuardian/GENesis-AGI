"""Count a PR's changed code lines and size band from its unified diff.

Takes the text of ``git diff -M <base>...<head>`` and returns a counted size:
added and removed lines excluding tests, changelog fragments, prose, binary
files, blanks, full-line comments, and lines moved verbatim elsewhere in the
same diff (a move counts once, as the addition).

Pure, stdlib-only, no subprocess — wiring it into ``gh pr create`` and the
merge report is maintainer work done elsewhere.
"""

from __future__ import annotations

import fnmatch
import re
from collections import Counter

SHAPE_AT = 500
OVERRIDE_AT = 1001

_PROSE_EXTS = {".md", ".rst", ".adoc", ".txt"}
_PROSE_NAMES = {"README", "LICENSE", "CHANGELOG", "NOTICE"}
_COMMENT_EXTS = {".py", ".sh", ".yaml", ".yml", ".toml", ".cfg", ".ini"}
_SLASH_COMMENT_EXTS = {".js", ".ts"}

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+\d+(?:,\d+)? @@")
_SHAPE_RE = re.compile(r"^\s*shape\s*:\s*(.*)$", re.IGNORECASE)
_FENCE_RE = re.compile(r"^\s*(```|~~~)")


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _exclusion_reason(path: str) -> str | None:
    """Why a file is excluded from the count, or None."""
    name = _basename(path)
    stem = name.rsplit(".", 1)[0] if "." in name else name
    if "tests" in path.split("/")[:-1] or fnmatch.fnmatchcase(name, "test_*.py") or fnmatch.fnmatchcase(
        name, "*_test.py"
    ):
        return "test"
    if path == "CHANGELOG.md" or "changelog.d" in path.split("/")[:-1]:
        return "changelog"
    ext = "." + name.rsplit(".", 1)[1] if "." in name else ""
    if ext in _PROSE_EXTS or (not ext and stem in _PROSE_NAMES):
        return "prose"
    return None


def _is_comment(content: str, path: str) -> bool:
    stripped = content.strip()
    name = _basename(path)
    ext = "." + name.rsplit(".", 1)[1] if "." in name else ""
    if stripped.startswith("#") and ext in _COMMENT_EXTS:
        return True
    return stripped.startswith("//") and ext in _SLASH_COMMENT_EXTS


class _FileState:
    def __init__(self, path: str) -> None:
        self.path = path
        self.binary = False
        self.unparseable = False
        self.in_hunk = False
        self.added: list[str] = []
        self.removed: list[str] = []


def count_diff(diff_text: str) -> dict:
    files: list[_FileState] = []
    current: _FileState | None = None

    def display_path() -> str:
        if current is None:
            return "<unknown>"
        return current.path

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            match = re.match(r"^diff --git a/(.+) b/(.+)$", line)
            files.append(_FileState(match.group(2) if match else line[11:]))
            current = files[-1]
            continue
        if current is None:
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            current.binary = True
            current.in_hunk = False
            continue
        if line.startswith("rename to "):
            current.path = line[len("rename to ") :]
            continue
        if line.startswith("+++ "):
            target = line[4:]
            if target.startswith("b/"):
                current.path = target[2:]
            current.in_hunk = False
            continue
        if line.startswith("@@"):
            if _HUNK_RE.match(line):
                current.in_hunk = True
            else:
                current.in_hunk = False
                current.unparseable = True
            continue
        if not current.in_hunk:
            continue
        if line.startswith(("+++", "---")):
            current.in_hunk = False
            continue
        if line.startswith("\\"):
            continue
        if line.startswith("+") or line.startswith("-"):
            content = line[1:]
            if not content.strip() or _is_comment(content, current.path):
                continue
            (current.added if line[0] == "+" else current.removed).append(content.strip())
            continue
        if line.startswith(" "):
            continue
        # A body line that is none of the above: the hunk is malformed.
        current.in_hunk = False
        current.unparseable = True

    counted_files: list[_FileState] = []
    excluded: dict[str, str] = {}
    for f in files:
        reason = _exclusion_reason(f.path)
        if f.binary:
            reason = reason or "binary"
        if f.unparseable:
            reason = reason or "unparseable"
        if reason:
            excluded[f.path] = reason
        else:
            counted_files.append(f)

    # Moves pair globally across the diff by multiset: a line removed anywhere
    # and added verbatim elsewhere counts once, as the addition.
    additions_pool = Counter(add for f in counted_files for add in f.added)
    moved = 0
    counted = 0
    by_file: dict[str, int] = {}
    for f in counted_files:
        file_count = len(f.added)
        for rem in f.removed:
            if additions_pool.get(rem, 0) > 0:
                additions_pool[rem] -= 1
                moved += 1
            else:
                file_count += 1
        counted += file_count
        if file_count:
            by_file[f.path] = file_count

    band = "ok" if counted < SHAPE_AT else "shape" if counted < OVERRIDE_AT else "override"
    return {
        "counted": counted,
        "by_file": by_file,
        "excluded": excluded,
        "moved": moved,
        "band": band,
    }


def parse_shape(body: str | None) -> str | None:
    """Return the text after a ``Shape:`` line, or None.

    Lines inside fenced code blocks are ignored; an empty value after the
    colon returns None.
    """
    if not body:
        return None
    fenced = False
    for line in body.splitlines():
        if _FENCE_RE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        match = _SHAPE_RE.match(line)
        if match:
            text = match.group(1).strip()
            return text or None
    return None
