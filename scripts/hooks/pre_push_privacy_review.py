#!/usr/bin/env python3
"""PreToolUse ADVISORY: surface private-data leaks in an outgoing PUBLIC push.

NON-BLOCKING. When a ``git push`` targets the public repo (origin), this hook
scans the diff being pushed for this install's private data — install IPs,
personal emails, and local release-fingerprints — and, if it finds any, injects
a review prompt into the model's context (PreToolUse ``additionalContext``). It
NEVER blocks the push: the CI ``leak-detector`` job and the branch/force gates
in ``git_push_guard.py`` own hard enforcement. Its job is to make the author
LOOK at suspicious lines before code goes public — the exact step that, when
skipped manually, put real install IPs into public test fixtures.

Reuses the contribution sanitizer's cheap REGEX scanners (``parse_diff`` +
``_check_portability`` / ``_check_emails`` / ``_check_fingerprints``) — NOT the
full ``scan_diff``, whose detect-secrets floor is fail-CLOSED (a false "missing
binary" finding on every push, since the venv bin isn't on the hook's PATH) and
whose secret scanners spawn one subprocess per added line (latency / timeout
kill on large diffs).

Contract: emits ONLY ``hookSpecificOutput.additionalContext`` on stdout and
ALWAYS exits 0. It carries no ``permissionDecision``, so it composes cleanly
with git_push_guard's ask/allow/deny on the same Bash matcher (each hook runs as
a separate process; additionalContext is concatenated, order-independent). Any
error → silent exit 0. An advisory must NEVER block a push.

Stdlib + the contribution sanitizer only.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path

# Self-locate so `from hook_input import …` resolves both when CC runs this as a
# script and when it is imported for tests (mirrors git_push_guard.py:27).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import field, read_payload  # noqa: E402

# Make `genesis.contribution.sanitize` importable. The genesis-hook wrapper runs
# the venv python where genesis is editable-installed, so the import normally
# resolves without this; the sys.path insert is a belt-and-suspenders fallback
# (mirrors credential_surface_hook.py). genesis/__init__ is empty and sanitize
# is stdlib-only at import time, so this is cheap (~150ms).
_SRC = Path(__file__).resolve().parents[2] / "src"
if _SRC.is_dir():
    sys.path.insert(0, str(_SRC))

_GIT_GLOBAL_VALUE_OPTS = ("-C", "-c", "--git-dir", "--work-tree", "--namespace")
_PUSH_VALUE_FLAGS = ("-o", "--push-option", "--repo", "--receive-pack", "--exec")
_MAX_LINES = 20
# Total wall-clock budget for ALL git calls in one invocation, and a per-call
# cap. A PreToolUse hook that TIMES OUT is treated by Claude Code as a BLOCK
# (an external process kill a Python try/except cannot catch), so the chain of
# git calls must finish comfortably under the hook's settings.json timeout (30s)
# even on a degraded-disk host. On budget exhaustion _git returns None → the
# scan is silently skipped (advisory degrades to quiet, never blocks).
_GIT_BUDGET_S = 12.0
_PER_CALL_TIMEOUT_S = 5.0
_deadline: float | None = None  # set per-invocation in main()


def _git(args: list[str], cwd: str | None) -> str | None:
    """Run a read-only git command; return stripped stdout, or None on failure.

    Bounded by the shared per-invocation ``_deadline`` (see _GIT_BUDGET_S) so
    the chained git calls can never approach the hook's CC-level timeout.
    """
    timeout = _PER_CALL_TIMEOUT_S
    if _deadline is not None:
        remaining = _deadline - time.monotonic()
        if remaining <= 0:
            return None
        timeout = min(timeout, remaining)
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd or None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _norm_url(url: str) -> str:
    """Normalize a git remote URL for comparison — drop the ``.git`` suffix,
    a trailing slash, and case, so textually-different spellings of the SAME
    repo (e.g. with/without ``.git``) still compare equal."""
    return re.sub(r"\.git$", "", url.strip().lower()).rstrip("/")


def _push_remote(cmd: str) -> str | None:
    """The remote/URL a ``git push`` targets, or None if cmd is not a git push.

    Returns "" for a bare ``git push`` (the branch's default push remote).
    Best-effort argv parse over shell segments; ambiguity resolves toward "" so
    the caller still scans (an advisory over-informs rather than misses).
    """
    for seg in re.split(r"\|\||&&|[;|&]", cmd):
        try:
            toks = shlex.split(seg)
        except ValueError:
            continue
        # Skip leading `VAR=val` env assignments, then require `git` as the
        # command word (so `echo git push` is not mistaken for a push).
        k = 0
        while k < len(toks) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[k]):
            k += 1
        if k >= len(toks) or toks[k] != "git":
            continue
        i = k + 1
        # Advance past git global options (and their values) to the subcommand.
        while i < len(toks) and toks[i].startswith("-"):
            if toks[i] in _GIT_GLOBAL_VALUE_OPTS and i + 1 < len(toks):
                i += 2
            else:
                i += 1
        if i >= len(toks) or toks[i] != "push":
            continue
        # First non-flag positional after `push` is the remote (or a URL).
        j = i + 1
        while j < len(toks):
            tok = toks[j]
            if tok.startswith("-"):
                if tok in _PUSH_VALUE_FLAGS and j + 1 < len(toks):
                    j += 2
                    continue
                j += 1
                continue
            return tok
        return ""  # bare push
    return None


def _targets_public_repo(remote: str, cwd: str | None) -> bool:
    """True if the push destination is origin (public), or is unresolvable.

    Advisory bias: scan on origin AND on anything we cannot confidently resolve
    to a NON-origin remote; skip only when the target clearly resolves to a
    different remote (e.g. a private fork), where real install IPs are allowed.
    """
    origin_url = _git(["remote", "get-url", "--push", "origin"], cwd)
    if remote == "":
        tracked = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{push}"], cwd)
        name = tracked.split("/", 1)[0] if tracked else "origin"
        target_url = _git(["remote", "get-url", "--push", name], cwd)
    elif "://" in remote or re.match(r"^[^/\s]+@[^/\s]+:", remote):
        target_url = remote  # literal URL / scp-like target
    else:
        target_url = _git(["remote", "get-url", "--push", remote], cwd)
    if not target_url or not origin_url:
        return True  # uncertain → inform (a public push is the risky case)
    return _norm_url(target_url) == _norm_url(origin_url)


def _outgoing_diff(cwd: str | None) -> str:
    """The unified diff being pushed (local commits not yet on origin)."""
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    base = None
    if (
        branch
        and branch != "HEAD"
        and _git(["rev-parse", "--verify", "--quiet", f"origin/{branch}"], cwd)
    ):
        base = f"origin/{branch}"
    else:
        base = _git(["merge-base", "origin/main", "HEAD"], cwd)
    if not base:
        return ""
    return _git(["diff", f"{base}..HEAD"], cwd) or ""


def _scan(diff_text: str) -> list:
    """Run only the cheap regex scanners from the contribution sanitizer."""
    from genesis.contribution import sanitize

    parsed = sanitize.parse_diff(diff_text)
    findings = list(sanitize._check_portability(parsed))
    findings += sanitize._check_emails(parsed)
    fp_env = os.environ.get("GENESIS_RELEASE_FINGERPRINTS")
    fp = Path(fp_env) if fp_env else Path.home() / ".genesis" / "release-fingerprints.txt"
    if fp.is_file():
        findings += sanitize._check_fingerprints(parsed, fp)
    return findings


def main() -> None:
    try:
        payload = read_payload()
        cmd = field(payload, "command")
        if not cmd:
            return
        remote = _push_remote(cmd)
        if remote is None:
            return  # not a git push
        # All git calls below share ONE wall-clock budget (see _GIT_BUDGET_S) so
        # the chain can never approach the hook's CC timeout (a timeout = block).
        global _deadline
        _deadline = time.monotonic() + _GIT_BUDGET_S
        cwd = payload.get("cwd") if isinstance(payload, dict) else None
        if not _targets_public_repo(remote, cwd):
            return  # private-fork / non-origin push — real IPs allowed there
        diff_text = _outgoing_diff(cwd)
        if not diff_text:
            return
        findings = _scan(diff_text)
        if not findings:
            return
        seen: set = set()
        lines: list[str] = []
        for finding in findings:
            key = (finding.file, finding.line, finding.message)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"  {finding.file or '?'}:{finding.line or '?'}  {finding.message}")
        context = (
            "[Pre-push privacy review] ⚠️ This push to the PUBLIC repo "
            "adds lines matching private-data patterns. Before it lands, confirm "
            "each is a generic placeholder (safe) or scrub the real value:\n"
            + "\n".join(lines[:_MAX_LINES])
        )
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": context,
                }
            },
            sys.stdout,
        )
    except Exception:
        # An advisory must NEVER block a push — swallow everything, exit 0.
        return


if __name__ == "__main__":
    main()
