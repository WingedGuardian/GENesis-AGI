#!/usr/bin/env python3
"""PreToolUse: refuse this install's OWN private values in a PUBLIC push; advise on the rest.

TWO VERDICTS, split by confidence — the distinction is the whole design:

* **DENY** when the diff contains a value this install actually HAS (a
  release-fingerprint literal: hostname, home path, subnet, tailnet address; or
  a personal email). There is no placeholder reading of such a match, so there
  is nothing for the author to confirm and no false positive to fear. Blocking
  here is the rare escalation the project's hook axioms allow, and it earns it
  by being unable to fire on correct code.
* **ADVISORY** for a generic RFC1918 / CGNAT / ULA-shaped literal. Those are
  legitimate constantly — ``192.168.1.5`` in a test is correct — so blocking
  them would make this a check that cries wolf, and a check that cries wolf gets
  ignored.

WHY IT CHANGED (2026-09-08): this hook previously never blocked, and flattened
both classes into one list at one severity. A push then carried a real peer
tailnet address and a real LAN address into public test fixtures. The hook fired
correctly and named every line — and was ignored, because an advisory's text
reaches the model in the SAME result as the completed push, so "confirm before
it lands" describes something an advisory cannot deliver. The second half of the
fix is in ``genesis.contribution.fingerprints``: the tailnet address that leaked
belonged to a PEER, so a local-interface harvest never knew it; the harvest now
covers the whole tailnet.

Hard enforcement elsewhere is unchanged and still the backstop: the CI
``leak-detector`` job and the branch/force gates in ``git_push_guard.py``. Note
CI runs at PR time while data goes public at PUSH time, which is the gap this
hook's DENY half now closes.

Reuses the contribution sanitizer's cheap REGEX scanners (``parse_diff`` +
``_check_portability`` / ``_check_emails`` / ``_check_fingerprints``) — NOT the
full ``scan_diff``, whose detect-secrets floor is fail-CLOSED (a false "missing
binary" finding on every push, since the venv bin isn't on the hook's PATH) and
whose secret scanners spawn one subprocess per added line (latency / timeout
kill on large diffs).

Contract: emits ``hookSpecificOutput`` on stdout and ALWAYS exits 0 — either a
``permissionDecision: deny`` (known-value match) or an ``additionalContext``
advisory (generic match only), never both. It composes with git_push_guard on
the same Bash matcher; a deny from either hook is decisive.

FAILS OPEN on an unexpected error (silent exit 0), deliberately: a bug in this
hook must not brick every push on the install, and the CI leak-detector remains
the backstop for anything it misses. That is a real limit of the guarantee, not
an oversight — the deny path is deterministic string matching precisely so the
failure surface stays small.

Stdlib + the contribution sanitizer only.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

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

# git global options that consume the FOLLOWING token as their value. Kept
# identical to the copies in git_push_guard/shell_parse/review_enforcement_commit
# (locked by tests/test_hooks/test_value_flag_consistency.py) — a missing member
# here silently skips the advisory scan on that command form.
_GIT_GLOBAL_VALUE_OPTS = (
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--super-prefix",
)
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


def _canonical_repo(url: str) -> tuple[str, str]:
    """``(host, path)`` for a git remote, identical across URL FORMS.

    MEASURED: ``_norm_url`` alone returns different strings for the same GitHub
    repo depending on whether it is written as HTTPS or as scp-style SSH —

        https://github.com/Owner/Repo.git  ->  https://github.com/owner/repo
        git@github.com:Owner/Repo.git      ->  git@github.com:owner/repo

    so a push to the public repo over SSH compared unequal to an HTTPS
    ``origin`` and the whole scan was skipped as "not the public repo". Adding a
    remote in the other form was a complete bypass, with no output at all.
    """
    raw = _norm_url(url)
    # scp-like: user@host:path (no scheme, single colon before the path)
    if "://" not in raw and ":" in raw:
        hostpart, _, path = raw.partition(":")
        host = hostpart.rpartition("@")[2]
        return host, path.strip("/")
    try:
        parts = urlsplit(raw if "://" in raw else f"ssh://{raw}")
    except ValueError:
        return "", raw
    return (parts.hostname or ""), parts.path.strip("/")


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


def _effective_cwd(cmd: str, payload_cwd: str | None) -> str | None:
    """The directory the ``git push`` actually runs in.

    Honors a preceding ``cd <dir>`` segment and a ``git -C <dir>`` on the push
    itself (either overrides the payload cwd) — so a ``git -C <worktree> push``
    or ``cd <worktree> && git push`` is scanned against the repo actually being
    pushed, not the hook's payload cwd. Falls back to ``payload_cwd``.
    """
    cwd = payload_cwd

    def _resolve(base: str | None, path: str) -> str:
        return path if os.path.isabs(path) else (os.path.join(base, path) if base else path)

    for seg in re.split(r"\|\||&&|[;|&]", cmd):
        try:
            toks = shlex.split(seg)
        except ValueError:
            continue
        if not toks:
            continue
        # A bare `cd <dir>` changes cwd for the following segments.
        if toks[0] == "cd" and len(toks) >= 2 and not toks[1].startswith("-"):
            cwd = _resolve(cwd, toks[1])
            continue
        k = 0
        while k < len(toks) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", toks[k]):
            k += 1
        if k >= len(toks) or toks[k] != "git":
            continue
        # Scan the git args for `-C <dir>` and whether the subcommand is push.
        c_dir: str | None = None
        m = k + 1
        while m < len(toks):
            tok = toks[m]
            if tok == "-C" and m + 1 < len(toks):
                c_dir = toks[m + 1]
                m += 2
                continue
            if tok.startswith("-"):
                if tok in _GIT_GLOBAL_VALUE_OPTS and m + 1 < len(toks):
                    m += 2
                    continue
                m += 1
                continue
            if tok == "push":
                return _resolve(cwd, c_dir) if c_dir else cwd
            break
    return cwd


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
    # Compare CANONICALLY (host, path), not as strings: the same repo written
    # as HTTPS and as scp-style SSH is the same destination, and comparing raw
    # strings let a second remote in the other form skip the scan entirely.
    return _canonical_repo(target_url) == _canonical_repo(origin_url)


def _push_source_ref(cmd: str) -> str | None:
    """The LOCAL ref a push actually sends, or None for "whatever HEAD is".

    ``git push origin some-branch:main`` and ``git push origin <sha>:refs/...``
    send a ref that is NOT the checked-out one, and the previous scan always
    diffed ``HEAD`` — so pushing a branch you had not checked out was scanned
    against unrelated content and sailed through with no signal at all.

    Returns None when there is no explicit refspec (scan HEAD, as before), and
    "" for a deletion (nothing is being sent, so nothing to scan).
    """
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return None
    try:
        i = argv.index("push")
    except ValueError:
        return None
    rest = [a for a in argv[i + 1 :]]
    if any(a in ("--delete", "-d") for a in rest):
        return ""  # deleting a remote ref sends no content
    positionals = [a for a in rest if not a.startswith("-")]
    # positionals: [remote] [refspec ...] — the refspec is the second onwards.
    if len(positionals) < 2:
        return None
    spec = positionals[1]
    if spec.startswith("+"):
        spec = spec[1:]
    src = spec.split(":", 1)[0]
    return src or ""


def _outgoing_diff(cwd: str | None, source_ref: str | None = None) -> str:
    """The unified diff being pushed (local commits not yet on origin).

    ``source_ref`` is the LOCAL ref actually being sent; None means HEAD.
    """
    branch = source_ref or _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
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


def _scan(diff_text: str) -> tuple[list, list]:
    """Split the cheap sanitizer scanners by CONFIDENCE, not into one flat list.

    Returns ``(known, generic)``:

    * ``known`` — matches against values this install actually HAS: its
      fingerprint patterns (hostnames, home path, subnets, tailnet addresses)
      and its personal emails. A literal equal to one of these is never correct
      in a public repo, so this half is refused rather than reported. Because
      the comparison is against a known literal, it cannot false-positive on a
      documentation placeholder.
    * ``generic`` — any RFC1918 / CGNAT / ULA-shaped literal. Legitimate all the
      time (``192.168.1.5`` in a test is fine), so this half stays advisory. A
      gate that fires on correct code gets ignored, and then it protects nothing.

    Keeping them apart is the whole point: the flat list this replaced rendered
    both at one severity, so the signal that mattered arrived wearing the same
    clothes as the noise.
    """
    from genesis.contribution import sanitize

    parsed = sanitize.parse_diff(diff_text)
    generic = list(sanitize._check_portability(parsed))
    known = list(sanitize._check_emails(parsed))
    fp_env = os.environ.get("GENESIS_RELEASE_FINGERPRINTS")
    fp = Path(fp_env) if fp_env else Path.home() / ".genesis" / "release-fingerprints.txt"
    if fp.is_file():
        known += sanitize._check_fingerprints(parsed, fp)
    return known, generic


def _render(findings: list) -> list[str]:
    """De-duplicated ``file:line  [category]`` lines — LOCATION, never CONTENT.

    Deliberately does NOT print the scanner's ``message``. A scanner is free to
    interpolate the offending literal into it (``_check_emails`` does exactly
    that: ``f"Personal email address in diff: {addr}"``), and ``detail`` carries
    the raw source line, so echoing either would make this hook publish the very
    value it is refusing — into the model's context and the session transcript,
    which is the one place a blocked secret must not land.

    Rendering the CATEGORY instead fixes the whole class rather than the one
    scanner that currently interpolates: a future scanner cannot leak through
    here by accident. The author already has the file and line, which is all
    that is needed to go look.

    The contribution CLI keeps printing the full message on purpose — it runs
    locally for a human who wants to see the value.
    """
    seen: set = set()
    lines: list[str] = []
    for finding in findings:
        kind = getattr(getattr(finding, "kind", None), "value", None) or getattr(
            finding, "scanner", None
        ) or "private-data"
        key = (finding.file, finding.line, kind)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"  {finding.file or '?'}:{finding.line or '?'}  [{kind}]")
    return lines


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
        payload_cwd = payload.get("cwd") if isinstance(payload, dict) else None
        # Resolve the repo the push ACTUALLY runs in — honoring `git -C <dir>`
        # and a preceding `cd <dir>` — so we scan the branch being pushed, not
        # the payload cwd (Codex P2 on #1267).
        cwd = _effective_cwd(cmd, payload_cwd)
        if not _targets_public_repo(remote, cwd):
            return  # private-fork / non-origin push — real IPs allowed there
        source_ref = _push_source_ref(cmd)
        if source_ref == "":
            return  # a deletion sends no content
        diff_text = _outgoing_diff(cwd, source_ref)
        if not diff_text:
            # Silence here used to be indistinguishable from "scanned, clean" —
            # but an unresolvable diff (shallow clone, origin/main never fetched,
            # a brand-new orphan branch) means NOTHING was scanned. Say which one
            # this is, so an author cannot read "no output" as "no findings".
            json.dump(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "additionalContext": (
                            "[Pre-push privacy review] Could not resolve the outgoing "
                            f"diff for '{source_ref or 'HEAD'}' — NOTHING was scanned "
                            "for private data on this push. Usually a shallow clone or "
                            "an origin/main that was never fetched. Treat this as "
                            "unchecked, not as clean."
                        ),
                    }
                },
                sys.stdout,
            )
            return
        known, generic = _scan(diff_text)
        if not known and not generic:
            return

        if known:
            # REFUSE. These are this install's own values, so there is no
            # placeholder reading available and nothing for the author to
            # confirm. Every other check here stays advisory; this one is the
            # exception, and it earns it by being unable to fire on correct code.
            reason = (
                "BLOCKED: this push would put THIS INSTALL'S OWN private values into "
                "the PUBLIC repo. These matched the install's known literals "
                "(fingerprints / personal email), not a generic pattern — so they "
                "are real, not placeholders:\n"
                + "\n".join(_render(known)[:_MAX_LINES])
                + "\n\nReplace each with a generic literal (a documentation address, "
                "an example.com email) and push again. If a value is genuinely "
                "meant to be public, remove it from "
                "~/.genesis/release-fingerprints.txt first — deliberately, not to "
                "get past this."
            )
            json.dump(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": reason,
                    }
                },
                sys.stdout,
            )
            return

        # Advisory half. Worded for what an advisory can actually achieve: it
        # carries no permissionDecision, so this text reaches the model in the
        # same result as the completed push. Telling the author to check
        # "before it lands" describes something the hook cannot deliver, and
        # invites exactly the false confidence that let a real value through.
        context = (
            "[Pre-push privacy review] This push to the PUBLIC repo adds lines "
            "matching GENERIC private-address patterns. They are usually fine — a "
            "documentation address in a test is correct — and none matched this "
            "install's own known values, which are refused outright rather than "
            "reported here. The push is going ahead; check these and scrub in a "
            "follow-up commit if any is real:\n"
            + "\n".join(_render(generic)[:_MAX_LINES])
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
    except Exception as exc:
        # Still fail OPEN — a bug here must not brick every push on the install,
        # and CI remains the backstop. But record it: a guard that has been
        # silently broken for weeks looks exactly like a guard that keeps finding
        # nothing, and nothing else would ever surface the difference. stdout is
        # reserved for the hook protocol, so this goes to a file.
        with contextlib.suppress(Exception):
            log = Path.home() / ".genesis" / "hook-errors.log"
            log.parent.mkdir(parents=True, exist_ok=True)
            with log.open("a", encoding="utf-8") as fh:
                fh.write(
                    f"{time.strftime('%Y-%m-%dT%H:%M:%S')} pre_push_privacy_review "
                    f"{type(exc).__name__}: {exc}\n"
                )
        return


if __name__ == "__main__":
    main()
