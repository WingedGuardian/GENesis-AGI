"""locate — deterministic self-retrieval over Genesis's known roots (Track 5.1).

A Genesis-owned MCP tool that surfaces recently-changed files (by mtime) across
known roots — plans, output, the repo, git worktrees — filtered by time window,
file type, scope, name glob, and optional content match, ranked by recency.

It exists because CC's Grep/Glob are flaky under the open-agents A/B bug; this is
a deterministic alternative for "what changed recently / where is X?".

STRICTLY READ-ONLY: only `os.scandir`/`stat`/`open`-for-read and `rg` (itself
read-only) are used. Nothing here writes, creates, or deletes.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from genesis import env

from ..memory import mcp

logger = logging.getLogger(__name__)

# Suffixes treated as code/config for the file_type filter (yaml/json/toml count
# as code/config; documented in the tool docstring).
_CODE_EXTS = frozenset({
    ".py", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".sh", ".bash", ".zsh",
    ".sql", ".c", ".h", ".cpp", ".hpp", ".cc", ".java", ".rb", ".php", ".lua",
    ".yaml", ".yml", ".toml", ".cfg", ".ini",
})

# Directory names whose entire subtree is skipped (build/dep/churn/binary noise).
# "worktrees" prunes ~/genesis/.claude/worktrees/* under the `repo` scope — those
# are reachable via the `worktrees` scope (git-discovered) instead.
_PRUNE_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".ruff_cache", ".mypy_cache", ".cache", "dist", "build", ".next", ".turbo",
    "target", "worktrees", "worktree-trash", "background-sessions", "cc-tmp",
    "embedding_cache", "qdrant_storage", "browser-profile", "camoufox-profile",
    "paste-cache", "file-history", "backups", "downloads",
})

# Per-file glob skips (binary/churn artifacts not worth surfacing).
_SKIP_FILE_GLOBS = ("*.db", "*.db-wal", "*.db-shm", "*.sqlite", "*.sqlite3", "*.lock")

_MAX_CONTENT_BYTES = 1_048_576       # 1 MiB — matches `rg --max-filesize 1M`
_BINARY_SNIFF_BYTES = 8192
_MAX_FILES_SCANNED = 50_000          # pathological-walk backstop
_CONTENT_POOL_CAP = 5_000            # most-recent N considered for content match
_HARD_LIMIT = 200                    # absolute cap on returned results
_SNIPPET_CHARS = 200
_MAX_MATCHES_PER_FILE = 10
_RG_CHUNK = 1_000                    # paths per rg invocation (ARG_MAX safety)

_VALID_SCOPES = ("docs", "plans", "output", "repo", "worktrees", "all")
_VALID_TYPES = ("any", "code", "noncode")


# ── small helpers ─────────────────────────────────────────────────────────────


def _have_rg() -> bool:
    """True if ripgrep is on PATH. Monkeypatchable in tests."""
    return shutil.which("rg") is not None


def _parse_window(within: str) -> float | None:
    """Parse a time-window token into seconds, or None for 'no limit'.

    Accepts ``Nh`` (hours), ``Nd`` (days), bare ``N`` (days), and
    ``0`` / ``""`` / ``all`` / ``any`` (no limit). Raises ValueError on garbage.
    """
    token = (within or "").strip().lower()
    if token in ("", "0", "all", "any"):
        return None
    if token.endswith("h"):
        seconds = float(token[:-1]) * 3600
    elif token.endswith("d"):
        seconds = float(token[:-1]) * 86400
    else:
        seconds = float(token) * 86400  # bare number = days; ValueError if non-numeric
    if seconds < 0:
        raise ValueError(f"negative time window: {within!r}")
    return seconds


def _humanize_age(mtime_epoch: float, now_epoch: float) -> str:
    diff = max(0.0, now_epoch - mtime_epoch)
    if diff < 60:
        return "just now"
    if diff < 3600:
        return f"{int(diff // 60)}m ago"
    if diff < 86400:
        return f"{int(diff // 3600)}h ago"
    return f"{int(diff // 86400)}d ago"


def _display(p: Path) -> str:
    s = str(p)
    home = str(Path.home())
    return "~" + s[len(home):] if s.startswith(home) else s


def _list_worktrees(repo_root: Path) -> list[Path]:
    """Worktree paths (excluding the main worktree) via git porcelain.

    Catches worktrees at every location (siblings, nested under the repo, /tmp).
    Returns [] on any error (not a git repo, git absent, timeout).
    """
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            capture_output=True, text=True, cwd=str(repo_root), timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    if result.returncode != 0:
        return []
    paths: list[Path] = []
    is_first = True
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            if is_first:
                is_first = False  # first entry is the main worktree — skip
            else:
                paths.append(Path(line[len("worktree "):]))
    return paths


def _resolve_roots(scope: str) -> list[tuple[str, Path]]:
    """Map a scope to (label, path) roots. Raises ValueError on unknown scope."""
    plans = ("plans", env.plans_dir())
    output = ("output", env.output_dir())
    repo = ("repo", env.repo_root())
    if scope == "plans":
        return [plans]
    if scope == "output":
        return [output]
    if scope == "docs":
        return [plans, output]
    if scope == "repo":
        return [repo]
    if scope == "worktrees":
        return [(f"worktree:{p.name}", p) for p in _list_worktrees(env.repo_root())]
    if scope == "all":
        wts = [(f"worktree:{p.name}", p) for p in _list_worktrees(env.repo_root())]
        return [plans, output, repo, *wts]
    raise ValueError(f"unknown scope: {scope!r}")


def _matches_skip_glob(name: str) -> bool:
    return any(fnmatch.fnmatch(name, g) for g in _SKIP_FILE_GLOBS)


# ── walk ──────────────────────────────────────────────────────────────────────


def _walk(
    label: str,
    root: Path,
    cutoff: float | None,
    file_type: str,
    name_glob: str,
    scan_budget: list[int],
) -> list[dict]:
    """Return file records under ``root`` passing recency/type/glob filters.

    Iterative, prune-aware, symlink-safe (never recurses into or lists symlinks).
    ``scan_budget`` is a single-element list mutated as a shared scanned-file
    counter / ceiling across roots.
    """
    records: list[dict] = []
    name_pat = name_glob.lower() if name_glob else ""
    stack: list[Path] = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except (PermissionError, OSError):
            continue
        for e in entries:
            if scan_budget[0] >= _MAX_FILES_SCANNED:
                return records
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name not in _PRUNE_DIRS:
                        stack.append(Path(e.path))
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue  # symlinks, sockets, fifos — skip
            except OSError:
                continue
            nm = e.name
            if _matches_skip_glob(nm):
                continue
            scan_budget[0] += 1
            if name_pat and not fnmatch.fnmatch(nm.lower(), name_pat):
                continue
            suffix = os.path.splitext(nm)[1].lower()
            if file_type == "code" and suffix not in _CODE_EXTS:
                continue
            if file_type == "noncode" and suffix in _CODE_EXTS:
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            if cutoff is not None and st.st_mtime < cutoff:
                continue
            records.append({
                "path": Path(e.path),
                "name": nm,
                "scope": label,
                "mtime": st.st_mtime,
                "size": st.st_size,
            })
    return records


# ── content match ─────────────────────────────────────────────────────────────


def _is_text_and_small(path: Path) -> bool:
    """True if the file is <= 1 MiB and not binary (no NUL in the first 8 KiB).

    Applied to BOTH backends before matching: rg bypasses its own binary/size
    filtering when files are passed as explicit path args (it only self-filters
    during recursive traversal), so we must gate candidates ourselves.
    """
    try:
        if path.stat().st_size > _MAX_CONTENT_BYTES:
            return False
        with path.open("rb") as fh:
            head = fh.read(_BINARY_SNIFF_BYTES)
    except OSError:
        return False
    return b"\x00" not in head


def _content_match(records: list[dict], contains: str, regex: bool) -> tuple[list[dict], str | None]:
    """Filter records to those whose content matches, attaching ``matches``.

    Returns (records_with_matches, error). ``error`` set only on invalid regex.
    Binary/oversize files are excluded up front (see ``_is_text_and_small``).
    Tries rg first (fast); falls back to pure Python when rg is absent or
    errors — identical result shape either way.
    """
    if regex:
        try:
            re.compile(contains)
        except re.error as exc:
            return [], f"invalid regex: {exc}"
    eligible = [r for r in records if _is_text_and_small(r["path"])]
    if _have_rg():
        out = _content_match_rg(eligible, contains, regex)
        if out is not None:
            return out, None
    return _content_match_python(eligible, contains, regex), None


def _content_match_rg(records: list[dict], contains: str, regex: bool) -> list[dict] | None:
    """rg-backed content match. Returns None to signal 'fall back to Python'."""
    by_path = {str(r["path"]): r for r in records}
    if not by_path:
        return []
    matches_by_path: dict[str, list[dict]] = {}
    paths = list(by_path.keys())
    base = ["rg", "--json", "--max-filesize", "1M"]
    if not regex:
        base.append("-F")
    base += ["-e", contains, "--"]
    for i in range(0, len(paths), _RG_CHUNK):
        chunk = paths[i:i + _RG_CHUNK]
        try:
            proc = subprocess.run(base + chunk, capture_output=True, text=True, timeout=60)
        except (subprocess.TimeoutExpired, OSError):
            return None  # fall back entirely for a consistent result
        if proc.returncode not in (0, 1):  # 0=matches, 1=no matches; >=2 = error
            return None
        for line in proc.stdout.splitlines():
            if not line:
                continue
            try:
                evt = json.loads(line)
            except ValueError:
                continue
            if evt.get("type") != "match":
                continue
            data = evt.get("data", {})
            p = data.get("path", {}).get("text")
            if p is None:
                continue
            lst = matches_by_path.setdefault(p, [])
            if len(lst) >= _MAX_MATCHES_PER_FILE:
                continue
            text = (data.get("lines", {}).get("text") or "").strip()[:_SNIPPET_CHARS]
            lst.append({"line": data.get("line_number"), "text": text})
    return [
        {**rec, "matches": matches_by_path[p]}
        for p, rec in by_path.items()
        if p in matches_by_path
    ]


def _content_match_python(records: list[dict], contains: str, regex: bool) -> list[dict]:
    """Pure-Python content match fallback (binary/oversize-guarded)."""
    pattern = re.compile(contains) if regex else None
    out: list[dict] = []
    for rec in records:
        path: Path = rec["path"]
        try:
            if path.stat().st_size > _MAX_CONTENT_BYTES:
                continue
            with path.open("rb") as fh:
                head = fh.read(_BINARY_SNIFF_BYTES)
            if b"\x00" in head:
                continue
            text = path.read_text(errors="replace")
        except OSError:
            continue
        matches: list[dict] = []
        for lineno, line in enumerate(text.splitlines(), start=1):
            hit = pattern.search(line) if pattern else (contains in line)
            if hit:
                matches.append({"line": lineno, "text": line.strip()[:_SNIPPET_CHARS]})
                if len(matches) >= _MAX_MATCHES_PER_FILE:
                    break
        if matches:
            out.append({**rec, "matches": matches})
    return out


# ── response assembly ─────────────────────────────────────────────────────────


def _error_response(query: dict, error: str, *, roots=None, skipped=None, scanned=0) -> dict:
    return {
        "summary": f"locate error: {error}",
        "query": query,
        "count": 0,
        "total_matched": 0,
        "truncated": False,
        "content_pool_capped": False,
        "scan_truncated": False,
        "scanned": scanned,
        "roots": roots or [],
        "skipped_roots": skipped or [],
        "results": [],
        "error": error,
    }


def _summarize(scope, within, file_type, contains, total, shown, truncated, skipped, capped, scan_truncated) -> str:
    extra = []
    if skipped:
        extra.append(f"{len(skipped)} root(s) absent")
    if capped:
        extra.append(f"content pool capped at {_CONTENT_POOL_CAP}")
    if scan_truncated:
        extra.append(
            f"scan hit the {_MAX_FILES_SCANNED}-file ceiling — results may be "
            "incomplete (narrow scope or within)"
        )
    suffix = (" · " + " · ".join(extra)) if extra else ""
    if total == 0:
        return f"no files match in scope '{scope}' (within {within}, {file_type})" + suffix
    head = f"{shown} of {total}" if truncated else f"{total}"
    quals = [f"within {within}", file_type]
    if contains:
        quals.append(f"containing {contains!r}")
    return f"{head} file(s) in '{scope}' ({', '.join(quals)})" + suffix


async def _impl_locate(
    scope: str = "docs",
    within: str = "24h",
    file_type: str = "any",
    name: str = "",
    contains: str = "",
    regex: bool = False,
    limit: int = 50,
) -> dict:
    now = time.time()
    scope = (scope or "docs").strip().lower()
    file_type = (file_type or "any").strip().lower()
    query = {
        "scope": scope, "within": within, "file_type": file_type,
        "name": name, "contains": contains, "regex": regex, "limit": limit,
    }

    if scope not in _VALID_SCOPES:
        return _error_response(query, f"unknown scope {scope!r}; valid: {', '.join(_VALID_SCOPES)}")
    if file_type not in _VALID_TYPES:
        return _error_response(query, f"unknown file_type {file_type!r}; valid: {', '.join(_VALID_TYPES)}")

    try:
        window_s = _parse_window(within)
    except ValueError:
        return _error_response(query, f"invalid 'within' value: {within!r}")
    cutoff = (now - window_s) if window_s is not None else None

    try:
        roots = _resolve_roots(scope)
    except ValueError as exc:
        return _error_response(query, str(exc))

    walked_roots: list[str] = []
    skipped_roots: list[str] = []
    scan_budget = [0]
    records: list[dict] = []
    for label, path in roots:
        if not path.is_dir():
            skipped_roots.append(_display(path))
            continue
        walked_roots.append(_display(path))
        records.extend(_walk(label, path, cutoff, file_type, name, scan_budget))

    scan_truncated = scan_budget[0] >= _MAX_FILES_SCANNED

    capped = False
    if contains:
        if len(records) > _CONTENT_POOL_CAP:
            records.sort(key=lambda r: (-r["mtime"], str(r["path"])))
            records = records[:_CONTENT_POOL_CAP]
            capped = True
        records, error = _content_match(records, contains, regex)
        if error:
            return _error_response(
                query, error, roots=walked_roots, skipped=skipped_roots, scanned=scan_budget[0],
            )

    records.sort(key=lambda r: (-r["mtime"], str(r["path"])))
    total = len(records)
    li = int(limit)
    eff_limit = _HARD_LIMIT if li <= 0 else min(li, _HARD_LIMIT)  # 0/neg = all up to cap
    top = records[:eff_limit]

    results: list[dict] = []
    for r in top:
        item = {
            "path": str(r["path"]),
            "name": r["name"],
            "scope": r["scope"],
            "mtime": datetime.fromtimestamp(r["mtime"], tz=UTC).isoformat(timespec="seconds"),
            "age": _humanize_age(r["mtime"], now),
            "size": r["size"],
        }
        if "matches" in r:
            item["matches"] = r["matches"]
        results.append(item)

    truncated = total > len(results)
    return {
        "summary": _summarize(
            scope, within, file_type, contains, total, len(results),
            truncated, skipped_roots, capped, scan_truncated,
        ),
        "query": query,
        "count": len(results),
        "total_matched": total,
        "truncated": truncated,
        "content_pool_capped": capped,
        "scan_truncated": scan_truncated,
        "scanned": scan_budget[0],
        "roots": walked_roots,
        "skipped_roots": skipped_roots,
        "results": results,
        "error": None,
    }


@mcp.tool()
async def locate(
    scope: str = "docs",
    within: str = "24h",
    file_type: str = "any",
    name: str = "",
    contains: str = "",
    regex: bool = False,
    limit: int = 50,
) -> dict:
    """Find recently-changed files across Genesis's known roots, ranked by recency.

    Deterministic, read-only self-retrieval — use this (not Grep/Glob) to answer
    "what working docs changed recently?" or "where is the file about X?".

    Args:
        scope: Which roots to search.
            "docs" (default) = ~/.claude/plans + ~/.genesis/output (fast, the
            working-docs case); "plans"; "output"; "repo" = the Genesis repo
            (pruned of .git/.venv/node_modules/worktrees/…); "worktrees" = all
            git worktrees; "all" = docs + repo + worktrees.
        within: Time window: "12h", "24h" (default), "7d", "30d", a bare number
            (days), or "0"/"all" for no time limit. Filters by file mtime.
        file_type: "any" (default), "code" (.py/.js/.ts/.go/.yaml/.json-ish/…),
            or "noncode" (.md/.txt/.pdf/… — the working-docs class).
        name: Case-insensitive glob on the filename, e.g. "*roadmap*.md". Empty = all.
        contains: Optional content match (only files passing the other filters are
            opened). Empty = no content filtering.
        regex: Treat ``contains`` as a regex (default: literal substring).
        limit: Max results (default 50, hard cap 200; 0 or negative = all up to
            the cap). Truncation is reported explicitly via count / total_matched
            / truncated.

    Returns a dict with: summary, query (echoed), count, total_matched, truncated,
    content_pool_capped, scan_truncated (true if the walk hit its file ceiling, so
    results may be incomplete), scanned, roots, skipped_roots, and results[] (path,
    name, scope, mtime, age, size, and matches[] when ``contains`` is used), newest first.
    """
    return await _impl_locate(
        scope=scope, within=within, file_type=file_type,
        name=name, contains=contains, regex=regex, limit=limit,
    )
