"""Count a PR's changed code lines and size band from its unified diff.

Takes the text of ``git diff -M <base>...<head>`` and returns a counted size:
added and removed lines excluding tests, changelog fragments, prose, binary
files, blanks, full-line comments, and lines moved verbatim elsewhere in the
same diff (a move counts once, as the addition).

Two classifiers are borrowed repo-locally rather than re-invented:
``review_scope.py`` supplies the file-kind judgements (its lane taxonomy is
the same one the review budget uses), and ``readable_body`` from
``check_cc_pin_receipts.py`` supplies PR-body visibility for ``parse_shape``,
so every body-reader in the repo shares one scanner. Both load through
``sys.modules``-registered importlib specs; a failed load raises
``RuntimeError`` rather than silently degrading into a second contract.

Pure functions otherwise, stdlib only (no third-party packages), no
subprocess — wiring it into ``gh pr create`` and the merge report is
maintainer work done elsewhere.
"""

from __future__ import annotations

import fnmatch
import importlib.util
import re
import sys
from collections import Counter
from pathlib import Path

SHAPE_AT = 500
OVERRIDE_AT = 1001

#: "" covers extensionless executables — the repo carries 13 tracked
#: extensionless shebang scripts (scripts/watchgod, scripts/hooks/pre-push…).
_COMMENT_EXTS = {".py", ".sh", ".yaml", ".yml", ".toml", ".cfg", ".ini", ""}
_SLASH_COMMENT_EXTS = {".js", ".ts"}

_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_QUOTED_TOKEN_RE = re.compile(r'^"(?:[^"\\]|\\.)*"')
_SHAPE_RE = re.compile(r"^\s*shape\s*:\s*(.*)$", re.IGNORECASE)


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


_MOD_CACHE: dict[str, object] = {}


def _load_sibling(filename: str, name: str):
    """Import a scripts/ sibling by file path, registered before exec.

    Registered in ``sys.modules`` BEFORE ``exec_module`` — dataclasses in the
    sibling resolve their own module out of sys.modules, so exec-ing an
    unregistered module raises. Cached; a failed load raises ``RuntimeError``
    instead of silently degrading into a second contract.
    """
    if name in _MOD_CACHE:
        return _MOD_CACHE[name]
    try:
        path = Path(__file__).resolve().parent / filename
        spec = importlib.util.spec_from_file_location(name, path)
        if not (spec and spec.loader):
            raise RuntimeError(f"cannot load {filename}")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            sys.modules.pop(name, None)
            raise
    except Exception as exc:
        raise RuntimeError(f"cannot load {filename}") from exc
    _MOD_CACHE[name] = mod
    return mod


def _exclusion_reason(path: str) -> str | None:
    """Why a file is excluded from the count, or None.

    Order: changelog, then test (fixture corpus, review_scope's category,
    or this counter's own basename/dir rule generalized past .py), then
    prose via review_scope's lane-light rule — which counts `.txt` files
    as code unless the stem is a prose stem (LICENSE, COPYING…).
    """
    scope = _load_sibling("review_scope.py", "_review_scope_for_pr_shape")
    name = _basename(path)
    if name == "CHANGELOG.md" or "changelog.d" in path.split("/")[:-1]:
        return "changelog"
    if (
        scope._is_lane_fixture_corpus(path)
        or scope._category(path) in ("test", "fixture")
        or "tests" in path.split("/")[:-1]
        or fnmatch.fnmatchcase(name, "test_*.*")
        or fnmatch.fnmatchcase(name, "*_test.*")
    ):
        return "test"
    if scope._is_lane_light(path):
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

    Handles Git's C-style escapes (``\\\\``, ``\\"``, ``\\t``, ``\\n``,
    ``\\a``, ``\\b``, ``\\v``, ``\\f``, ``\\r``) and ``\\ooo`` octal bytes,
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
                    {
                        "n": b"\n",
                        "t": b"\t",
                        "a": b"\a",
                        "b": b"\b",
                        "v": b"\v",
                        "f": b"\f",
                        "r": b"\r",
                        "\\": b"\\",
                        '"': b'"',
                    }.get(nxt, b"\\" + nxt.encode("utf-8", "replace"))
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

    if not rest.startswith('"'):
        # Same-path file with spaces arrives unquoted as `a/P b/P`; renames
        # with spaces are quoted by git, so a symmetric pair is one path.
        k, rem = divmod(len(rest) - 5, 2)
        if rem == 0 and rest[2 + k : 5 + k] == " b/" and rest[2 : 2 + k] == rest[5 + k :]:
            return rest[5 + k :], rest[5 + k :]

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
        self.binary = self.unparseable = self.in_hunk = self.seen_hunk = False
        self.old_rem = self.new_rem = 0
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
                # Removed lines are classified against the PRE-rename path:
                # a `-# comment` in old.py stays a comment even when the file
                # is being renamed to new.js.
                old = current.old_path or current.path
                if content.strip() and not _is_comment(content, old):
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
                current.seen_hunk = True
            else:
                current.unparseable = True
            continue
        if current.seen_hunk and line.startswith(("+", "-")):
            # A change line after the file's hunks have all completed: the
            # `---`/`+++` headers belong BEFORE the first `@@`; afterwards a
            # `+`/`-` line outside a hunk means the diff is malformed.
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

    Visibility (fences, HTML comments) is the shared ``readable_body`` — a
    fenced ``Shape:`` is documentation, not an assertion. An empty value
    masks nothing: the first non-empty one wins.
    """
    if not body:
        return None
    sibling = _load_sibling("check_cc_pin_receipts.py", "_cc_pin_receipts_for_pr_shape")
    for line in sibling.readable_body(body).split("\n"):
        match = _SHAPE_RE.match(line)
        if match:
            # Invisible Unicode format chars (Cf — zero-width space, joiners,
            # bidi controls) are not content; the sibling owns the stripper.
            value = sibling._strip_formatting_chars(match.group(1)).strip()
            if value:
                return value
    return None
