#!/usr/bin/env python3
"""PreToolUse hook: catch wrong-repo ``git add`` / ``git commit``.

The OMI incident (2026-07): a session built edge-device software (esphome /
firmware / s2s_bridge) into the AGI codebase, blind to the separate
GENesis-Voice repo. This hook is the deterministic backstop — it identifies the
CURRENT repo by its ``origin`` remote, then flags staged/added files whose path
(or, for new files, content) belongs to a DIFFERENT repo per
``repo_topology.yaml``.

Contract (matches the other Genesis PreToolUse hooks):
- Input via ``CLAUDE_TOOL_INPUT`` env (JSON with a ``command`` field).
- STRONG match -> BLOCK: message to stderr, exit 2.
- WEAK match  -> ADVISORY: ``hookSpecificOutput.additionalContext`` on stdout, exit 0.
- No match / not a git add|commit / anything unexpected -> exit 0 (fail-open).
- Override: append ``# repo-routing-override`` to the command to bypass.

Fail-open is load-bearing: this hook must NEVER block a legitimate commit. Every
failure path returns 0. The WS-C ambient-awareness layer is the upstream net;
this guard is a precise, deterministic backstop, not a wall.

Known limits (accepted, documented):
- ``bash -c "..."`` nesting or multi-``cd`` chains can defeat cwd inference; the
  guard fails open in those cases (the ambient lane is the net).
- ``git commit --amend`` sees only the staged delta, not the files already in
  the commit being amended.
- Topology is matched by ``origin`` remote; a repo with no origin is not guarded.
- Exotic command shapes degrade in the FAIL-SAFE direction (broader scan, never
  a missed detection): a pathspec/message token literally equal to ``git``/``cd``
  truncates arg collection, and space-separated global flags (``git --git-dir P
  commit``) are not value-skipped like ``-C``/``-c``. Agent-issued commands here
  use ``git -C`` / ``cd && git``, both handled and tested.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

_OVERRIDE_RE = re.compile(r"#\s*repo-routing-override\b")


def _load_topology() -> dict | None:
    """Load repo topology from the first available source. None on any failure."""
    candidates = []
    env_path = os.environ.get("REPO_TOPOLOGY_PATH")
    if env_path:
        candidates.append(Path(env_path))
    candidates.append(Path.home() / ".genesis" / "config" / "repo_topology.yaml")
    # In-repo fallback: this file lives at <root>/scripts/hooks/, config at <root>/config/.
    candidates.append(Path(__file__).resolve().parents[2] / "config" / "repo_topology.yaml")

    path = next((p for p in candidates if p.is_file()), None)
    if path is None:
        return None
    try:
        import yaml
    except ImportError:
        # Visible degradation, not silent — the guard is off without a parser.
        print(
            "NOTE: repo_routing_guard disabled — PyYAML unavailable in hook runtime.",
            file=sys.stderr,
        )
        return None
    try:
        with open(path) as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


_SHELL_OPS = {"&&", "||", ";", "|", "&"}


def _parse_git_invocations(cmd: str) -> list[tuple[str, list[str], str]]:
    """Return every git add|commit in the command as (subcommand, args, dir).

    Tokenizes the WHOLE command once with shlex (quote-aware, so a ``;`` inside a
    ``-m`` message is not a separator, and newlines collapse to whitespace), then
    walks the token stream: shell operators separate commands, ``cd <dir>`` and
    ``git -C <dir>`` update the effective directory, and EVERY add/commit is
    collected (not just the first). Best-effort — an unparseable command yields [].
    """
    try:
        toks = shlex.split(cmd)
    except ValueError:
        return []
    out: list[tuple[str, list[str], str]] = []
    cur_dir = os.getcwd()
    i, n = 0, len(toks)
    while i < n:
        t = toks[i]
        if t in _SHELL_OPS:
            i += 1
            continue
        if t == "cd" and i + 1 < n and toks[i + 1] not in _SHELL_OPS:
            d = os.path.expanduser(toks[i + 1])
            cur_dir = d if os.path.isabs(d) else os.path.normpath(os.path.join(cur_dir, d))
            i += 2
            continue
        if t == "git":
            i += 1
            git_dir = cur_dir
            while i < n:
                if toks[i] == "-C" and i + 1 < n:
                    d = os.path.expanduser(toks[i + 1])
                    git_dir = d if os.path.isabs(d) else os.path.normpath(os.path.join(cur_dir, d))
                    i += 2
                    continue
                if toks[i] == "-c" and i + 1 < n:
                    i += 2
                    continue
                if toks[i].startswith("-"):
                    i += 1
                    continue
                break
            sub = toks[i] if i < n else None
            if sub is not None:
                i += 1
            # Collect this git command's args until the next command boundary.
            args: list[str] = []
            while i < n and toks[i] not in _SHELL_OPS and toks[i] not in ("git", "cd"):
                args.append(toks[i])
                i += 1
            if sub in ("add", "commit"):
                out.append((sub, args, git_dir))
            continue
        i += 1
    return out


def _git(git_dir: str, args: list[str], timeout: int) -> str:
    r = subprocess.run(
        ["git", "-C", git_dir, *args],
        capture_output=True, text=True, timeout=timeout,
    )
    return r.stdout if r.returncode == 0 else ""


def _normalize_remote(url: str | None) -> str | None:
    """Normalize a remote to ``owner/name`` (lowercased).

    Handles full URLs and the bare ``owner/name`` form used in the topology:
      git@github.com:Owner/Name.git | https://github.com/Owner/Name | Owner/Name
    """
    if not url:
        return None
    s = re.sub(r"\.git$", "", url.strip().rstrip("/"))
    parts = [p for p in re.split(r"[/:]", s) if p]
    return "/".join(parts[-2:]).lower() if len(parts) >= 2 else None


def _porcelain_files(
    git_dir: str, pathspecs: list[str], timeout: int,
) -> tuple[set[str], set[str]]:
    """(all_files, new_files) that a status/add would touch, via git porcelain."""
    # --untracked-files=all expands fully-untracked directories into individual
    # file paths (porcelain otherwise collapses them to "dir/"), which we need
    # for per-file classification and content-marker reads.
    args = ["status", "--porcelain", "--untracked-files=all"]
    if pathspecs:
        args += ["--", *pathspecs]
    out = _git(git_dir, args, timeout)
    files, new = set(), set()
    for line in out.splitlines():
        if len(line) < 4:
            continue
        xy, path = line[:2], line[3:]
        # Deletions can never INTRODUCE wrong-repo content — skip so that
        # removing a foreign file (the incident cleanup) is never blocked.
        if "D" in xy:
            continue
        if " -> " in path:  # rename — classify the destination path
            path = path.split(" -> ", 1)[1]
        path = path.strip().strip('"')
        if not path:
            continue
        files.add(path)
        if xy.strip() in ("A", "??") or xy[0] == "A":
            new.add(path)
    return files, new


def _commit_files(
    git_dir: str, args: list[str], timeout: int,
) -> tuple[set[str], set[str]]:
    """(all_files, new_files) a commit would include."""
    files, new = set(), set()
    for line in _git(git_dir, ["diff", "--cached", "--name-status"], timeout).splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0], parts[-1].strip().strip('"')
        if status.startswith("D"):  # deletion — cannot introduce foreign content
            continue
        files.add(path)
        if status.startswith("A"):
            new.add(path)
    # `commit -a` / `-am` also stages tracked modifications (never new files).
    if any(a in ("-a", "--all") or (a.startswith("-") and not a.startswith("--") and "a" in a)
           for a in args):
        for line in _git(git_dir, ["diff", "HEAD", "--name-status"], timeout).splitlines():
            parts = line.split("\t")
            if len(parts) < 2 or parts[0].startswith("D"):
                continue
            files.add(parts[-1].strip().strip('"'))
    return files, new


def _add_files(
    git_dir: str, args: list[str], timeout: int,
) -> tuple[set[str], set[str]]:
    pathspecs = [a for a in args if not a.startswith("-")]
    broad = (not pathspecs) or any(p in (".", ":/", "-A", "--all") for p in pathspecs)
    return _porcelain_files(git_dir, [] if broad else pathspecs, timeout)


def _path_segments(path: str) -> set[str]:
    return set(path.replace("\\", "/").split("/"))


def _glob_match(path: str, glob: str) -> bool:
    base = os.path.basename(path)
    tail = glob.rsplit("/", 1)[-1]
    return (
        fnmatch.fnmatch(path, glob)
        or fnmatch.fnmatch(path, glob.replace("**/", "*"))
        or ("**/" in glob and fnmatch.fnmatch(base, tail))
    )


def _matches_strong_path(path: str, sig: dict) -> bool:
    if _path_segments(path) & set(sig.get("strong_path_segments", []) or []):
        return True
    return any(_glob_match(path, g) for g in (sig.get("strong_path_globs", []) or []))


def _is_allowed(path: str, allow_paths: list[str]) -> bool:
    for a in allow_paths or []:
        a = a.rstrip("/")
        if path == a or path.startswith(a + "/"):
            return True
    return False


def _has_content_marker(git_dir: str, path: str, markers: list[str], max_bytes: int) -> bool:
    try:
        with open(os.path.join(git_dir, path), "rb") as f:
            blob = f.read(max_bytes).decode("utf-8", "ignore")
    except OSError:
        return False
    return any(m in blob for m in markers)


_DEFAULT_CONTENT_EXCLUDE = [
    "*.md", "**/*.md", "docs/**", "tests/**", "**/repo_topology.yaml",
]


def _path_excluded(path: str, globs: list[str]) -> bool:
    return any(_glob_match(path, g) for g in globs)


def _classify(
    files: set[str], new_files: set[str], git_dir: str,
    cur_allow: list[str], foreign: dict[str, dict], settings: dict,
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Return (strong_hits, weak_hits) as lists of (path, foreign_repo_name)."""
    max_files = int(settings.get("max_files", 200))
    max_content = int(settings.get("max_content_files", 20))
    max_bytes = int(settings.get("max_content_bytes", 4096))
    # Content markers scan CODE, not prose/tests/the ruleset itself — otherwise
    # this guard's own topology + test files (which name the markers verbatim)
    # would self-block on a fresh add (e.g. the public-repo distribution).
    content_exclude = settings.get("content_scan_exclude") or _DEFAULT_CONTENT_EXCLUDE
    strong: list[tuple[str, str]] = []
    weak: list[tuple[str, str]] = []
    reads = 0
    for path in sorted(files)[:max_files]:
        matched_strong = False
        for name, sig in foreign.items():
            if _matches_strong_path(path, sig):
                strong.append((path, name))
                matched_strong = True
                break
            if (path in new_files and reads < max_content
                    and not _path_excluded(path, content_exclude)
                    and _has_content_marker(
                        git_dir, path, sig.get("strong_content_markers", []) or [], max_bytes)):
                reads += 1
                strong.append((path, name))
                matched_strong = True
                break
        if matched_strong:
            continue
        if _is_allowed(path, cur_allow):
            continue  # allow_paths silence WEAK markers
        for name, sig in foreign.items():
            if _path_segments(path) & set(sig.get("weak_path_segments", []) or []):
                weak.append((path, name))
                break
    return strong, weak


def main() -> int:
    try:
        raw = os.environ.get("CLAUDE_TOOL_INPUT", "")
        if not raw:
            return 0
        cmd = (json.loads(raw) or {}).get("command", "")
        # Cheap pre-filter: skip clearly non-git commands. Do NOT match "git add"
        # as a substring — `git -C <dir> add` splits it. _parse_git_invocation
        # is the accurate check (returns None for non add/commit).
        if not cmd or "git" not in cmd:
            return 0

        if _OVERRIDE_RE.search(cmd):
            print("NOTE: repo-routing-guard override acknowledged by session.", file=sys.stderr)
            return 0

        invocations = _parse_git_invocations(cmd)
        if not invocations:
            return 0

        topo = _load_topology()
        if not topo or not isinstance(topo.get("repos"), dict):
            return 0
        repos = topo["repos"]
        settings = topo.get("settings", {}) or {}
        timeout = int(settings.get("git_timeout_seconds", 2))

        # Accumulate hits across EVERY git add/commit in the command (chained
        # commands each get classified against their own effective repo).
        strong: list[tuple[str, str]] = []
        weak: list[tuple[str, str]] = []
        cur_key = None
        for sub, args, git_dir in invocations:
            origin = _normalize_remote(
                _git(git_dir, ["config", "--get", "remote.origin.url"], timeout).strip())
            if not origin:
                continue
            key = next(
                (k for k, v in repos.items()
                 if origin in {_normalize_remote(r) for r in (v.get("remotes") or [])}),
                None,
            )
            if key is None:
                continue  # unknown repo — don't guard this invocation
            foreign = {
                k: v for k, v in repos.items()
                if k != key and (
                    v.get("strong_path_segments") or v.get("strong_path_globs")
                    or v.get("strong_content_markers") or v.get("weak_path_segments"))
            }
            if not foreign:
                continue
            if sub == "commit":
                files, new_files = _commit_files(git_dir, args, timeout)
            else:
                files, new_files = _add_files(git_dir, args, timeout)
            if not files:
                continue
            cur_allow = repos.get(key, {}).get("allow_paths", []) or []
            s, w = _classify(files, new_files, git_dir, cur_allow, foreign, settings)
            strong.extend(s)
            weak.extend(w)
            cur_key = key

        # A file staged then committed in one command is hit twice — dedup,
        # preserving order, and drop weak hits already flagged as strong.
        strong = list(dict.fromkeys(strong))
        strong_paths = {p for p, _ in strong}
        weak = [h for h in dict.fromkeys(weak) if h[0] not in strong_paths]

        if strong:
            belongs = sorted({name for _, name in strong})
            shown = [p for p, _ in strong][:8]
            print(
                f"BLOCKED: wrong-repo commit. These files belong to "
                f"{', '.join(belongs)}, not {cur_key}:",
                file=sys.stderr,
            )
            for p in shown:
                print(f"  - {p}", file=sys.stderr)
            if len(strong) > len(shown):
                print(f"  … and {len(strong) - len(shown)} more", file=sys.stderr)
            print(
                f"Commit this in the {belongs[0]} repo, or append "
                f"'# repo-routing-override' to proceed if this is intentional.",
                file=sys.stderr,
            )
            return 2

        if weak:
            belongs = sorted({name for _, name in weak})
            shown = ", ".join(p for p, _ in weak[:5])
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": (
                        f"NOTE: {len(weak)} file(s) look voice/edge-related "
                        f"({shown}) and may belong to {', '.join(belongs)} rather "
                        f"than {cur_key}. Proceed if this is legitimately internal "
                        f"channel code; otherwise commit it in the right repo."
                    ),
                }
            }))
            return 0

    except Exception:
        return 0  # fail-open — never block legitimate work
    return 0


if __name__ == "__main__":
    sys.exit(main())
