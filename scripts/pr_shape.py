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

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_QUOTED_TOKEN_RE = re.compile(r'^"(?:[^"\\]|\\.)*"')
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


def _unquote(s: str) -> str:
    """Decode a Git C-quoted path; unquoted input passes through.

    Handles ``\\\\``, ``\\"``, ``\\t``, ``\\n`` and ``\\ooo`` octal byte escapes,
    then UTF-8 decodes with replacement. Never raises.
    """
    try:
        if len(s) < 2 or not (s.startswith('"') and s.endswith('"')):
            return s
        inner = s[1:-1]
        out = bytearray()
        i = 0
        while i < len(inner):
            c = inner[i]
            if c == "\\" and i + 1 < len(inner):
                nxt = inner[i + 1]
                if nxt in "01234567":
                    j = i + 1
                    while j < len(inner) and inner[j] in "01234567" and j - i <= 3:
                        j += 1
                    out.append(int(inner[i + 1 : j], 8) & 0xFF)
                    i = j
                    continue
                out.extend(
                    {"n": b"\n", "t": b"\t", "\\": b"\\", '"': b'"'}.get(
                        nxt, b"\\" + nxt.encode("utf-8", "replace")
                    )
                )
                i += 2
                continue
            out.extend(c.encode("utf-8", "replace"))
            i += 1
        return bytes(out).decode("utf-8", "replace")
    except Exception:
        return s


def _diff_git_paths(rest: str) -> tuple[str | None, str | None]:
    """Split ``a/<old> b/<new>`` (either side possibly C-quoted) into paths."""

    def token(s: str) -> tuple[str, str]:
        if s.startswith('"'):
            m = _QUOTED_TOKEN_RE.match(s)
            if m:
                return m.group(0), s[m.end() :]
            return s, ""
        head, _, tail = s.partition(" ")
        return head, tail

    a_tok, rem = token(rest)
    b_tok, _ = token(rem.strip())

    def strip_side(tok: str, prefix: str) -> str | None:
        path = _unquote(tok)
        return path[len(prefix) :] if path.startswith(prefix) else None

    return strip_side(a_tok, "a/"), strip_side(b_tok, "b/")


class _FileState:
    def __init__(self, fallback: str) -> None:
        self.fallback = fallback
        self.new_path: str | None = None
        self.old_path: str | None = None
        self.rename_to: str | None = None
        self.binary = False
        self.unparseable = False
        self.in_hunk = False
        self.old_rem = 0
        self.new_rem = 0
        self.added: list[str] = []
        self.removed: list[str] = []

    @property
    def path(self) -> str:
        # `+++ b/` wins; `--- a/` carries deletions (`+++ /dev/null`); then
        # `rename to`; the `diff --git` b-side is the last resort.
        return self.new_path or self.old_path or self.rename_to or self.fallback


def count_diff(diff_text: str) -> dict:
    files: list[_FileState] = []
    current: _FileState | None = None

    # splitlines() would break on Unicode separators (U+2028, U+2029…) — a
    # line containing one must stay ONE line, so split on "\n" only and strip
    # at most one trailing "\r" per line.
    for line in diff_text.split("\n"):
        if line.endswith("\r"):
            line = line[:-1]
        if line.startswith("diff --git "):
            # A new file while hunk counts remain: the previous file is
            # unparseable (its hunk was truncated).
            if current is not None and current.in_hunk:
                current.unparseable = True
            _a, b = _diff_git_paths(line[len("diff --git ") :])
            files.append(_FileState(b if b is not None else line[len("diff --git ") :]))
            current = files[-1]
            continue
        if current is None:
            continue
        if current.in_hunk:
            # Inside a hunk EVERYTHING is content — `+++`/`---`/`diff --git`-looking
            # lines are counted like any other added/removed/context line until the
            # header counts run out.
            if line.startswith("\\"):
                continue  # "\ No newline at end of file"
            first = line[0] if line else " "
            if first == "-":
                current.old_rem -= 1
                content = line[1:]
                if content.strip() and not _is_comment(content, current.path):
                    current.removed.append(content.strip())
            elif first == "+":
                current.new_rem -= 1
                content = line[1:]
                if content.strip() and not _is_comment(content, current.path):
                    current.added.append(content.strip())
            elif first == " " or line == "":
                current.old_rem -= 1
                current.new_rem -= 1
            else:
                current.unparseable = True
                current.in_hunk = False
                continue
            if current.old_rem < 0 or current.new_rem < 0:
                current.unparseable = True
                current.in_hunk = False
            elif current.old_rem == 0 and current.new_rem == 0:
                current.in_hunk = False
            continue
        if line.startswith("Binary files ") or line.startswith("GIT binary patch"):
            current.binary = True
            continue
        if line.startswith("rename to "):
            current.rename_to = _unquote(line[len("rename to ") :].strip())
            continue
        if line.startswith("--- "):
            path = _unquote(line[4:].strip())
            if path != "/dev/null":
                current.old_path = path[2:] if path.startswith("a/") else path
            continue
        if line.startswith("+++ "):
            path = _unquote(line[4:].strip())
            if path != "/dev/null":
                current.new_path = path[2:] if path.startswith("b/") else path
            continue
        if line.startswith("@@"):
            m = _HUNK_RE.match(line)
            if m:
                current.old_rem = int(m.group(2) or 1)
                current.new_rem = int(m.group(4) or 1)
                current.in_hunk = current.old_rem > 0 or current.new_rem > 0
            else:
                current.unparseable = True
            continue

    # A file still inside a hunk at EOF was truncated mid-hunk.
    if current is not None and current.in_hunk:
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
    fence: str | None = None
    for line in body.splitlines():
        m = _FENCE_RE.match(line)
        if fence is None:
            if m:
                fence = m.group(1)
            else:
                match = _SHAPE_RE.match(line)
                # An empty Shape: value masks nothing — keep scanning for the
                # first non-empty one.
                if match and match.group(1).strip():
                    return match.group(1).strip()
        elif m and m.group(1) == fence:
            fence = None
    return None
