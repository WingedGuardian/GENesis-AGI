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


# The push's directory could not be determined from the command text — a shell
# variable, a command substitution, a glob, `cd -`, a bare `cd`, or a relative
# `cd` with no payload cwd to resolve against.
#
# ADVISORY semantics, and they INVERT the guards' meaning of the same idea: in
# git_push_guard an unresolvable cwd means "fail closed / block", because that
# hook authorizes. This one only informs, so here it means "say so" — never
# block, and above all never fabricate a path.
#
# Fabricating is what it used to do: an unresolved target was joined onto the
# payload cwd, yielding a directory that cannot exist (``<cwd>/$W``). Every git
# call against it then failed, ``_outgoing_diff`` returned "", and ``main``
# returned with NO output — so a push this hook could not scope was
# indistinguishable from a clean one.
#
# MEASURED on this install's 708 real pushes, by replaying each through the OLD
# resolver and asking whether its answer could name a directory on ANY day:
#   175 (24.7%)  impossible because `~` was never expanded  <- the big one
#    20  (2.8%)  impossible because `$`/backtick was never expanded
#   ---
#   195 (27.5%)  total, every one of which scanned nothing, silently
# The tilde class is the larger by ~9x, which is why the fix is not just about
# shell variables.
#
# The test is STRUCTURAL — does the answer still carry a `~`, `$`, backtick or
# glob — not `isdir`. `isdir` is evaluated today, so a historical push into a
# since-deleted worktree would score as impossible although it resolved
# perfectly at the time; that artifact reads as 46.3% and would have been wrong.
#
# AFTER, on the same corpus: 664/708 (93.8%) answer a concrete directory,
# 0/708 answer an impossible one, and 44/708 (6.2%) raise this notice. The
# earlier figure of 2.6% counted only the shell-variable class, not the notice.
_CWD_UNRESOLVED = object()

# Every character bash would ACT on, so a literal read of the token would check a
# DIFFERENT directory than the one bash enters: expansion triggers (`$` parameter,
# backtick command-substitution, `{` brace, `\` escape), globs (`*?[`), and the
# in-segment metacharacters `()<>` (e.g. process substitution). Taken verbatim from
# review_enforcement_commit._cd_target, the most evolved of the sibling copies —
# an earlier version of this hook used only "$`*?" and therefore still resolved
# `cd /a/b[1]` to a literal path that cannot exist, which failed SILENTLY.
# Kept in step by tests/test_hooks/test_value_flag_consistency.py.
#
# `\` is deliberately ABSENT from this set, and that is a real divergence from
# the raw-segment copies rather than an oversight. They read the segment before
# any shell processing, so a backslash is still ambiguous to them and they refuse.
# This copy is handed an already-shlex-split token, and shlex has ALREADY applied
# bash's escape semantics — `cd /a/b\c` arrives as `/a/bc`, which is precisely the
# directory bash enters. Keeping `\` here would be dead (the token can no longer
# contain one) and would refuse a path we resolve correctly. The positive
# is-it-a-real-directory check in main() backstops any residual mismatch.
_UNRESOLVABLE_CD_CHARS = "$`*?[{()<>"


def _cd_target(token: str | None, base=None):
    """The directory a ``cd`` moves to, or ``_CWD_UNRESOLVED`` if unknowable.

    ``token`` is already shlex-split, so quotes are gone: ``cd "$W"`` and
    ``cd $W`` both arrive as ``$W``. That is why the check is on the characters
    rather than on the quoting — and why a token that still contains whitespace
    got there from a QUOTED path, which is faithful and therefore allowed.

    DELIBERATE DIVERGENCE from the sibling copies, recorded rather than silent:
    an unresolvable ``~user`` returns UNRESOLVED here, where they return the
    literal ``~user/...``. Theirs then resolves to a path that cannot exist,
    which for an advisory means a silent no-op; refusing to guess is strictly
    safer and costs only a notice. See the divergence table in the parity lock.
    """
    if token is None:
        return _CWD_UNRESOLVED  # bare `cd` -> $HOME; not knowable from the text
    if token.startswith("-"):
        return _CWD_UNRESOLVED  # `cd -` (previous dir), or an option like `-P`
    if token.startswith("~"):
        # Expand BEFORE the metacharacter check, as the siblings do: `~` is not
        # itself in the set, and expanding first is what makes `cd ~/wt` resolve.
        expanded = os.path.expanduser(token)
        if expanded.startswith("~"):
            return _CWD_UNRESOLVED  # `~otheruser` the passwd db cannot resolve
        # shlex has already discarded the quoting, and bash expands `~` ONLY
        # when it is UNQUOTED: `cd "~/wt"` and `cd '~/wt'` enter a LITERAL
        # directory named `~/wt`. Both readings are therefore candidates here,
        # and the token alone cannot say which bash took.
        #
        # Refusing every `~` was the first proposal. MEASURED against 708 real
        # pushes from this install's corpus: 172 (24.3%) cd to a `~` target, and
        # 0 of them have an existing literal counterpart. So refusing outright
        # would cost a QUARTER of all pushes their scan — the coverage this hook
        # was repaired to provide — to close a case that does not occur. It is
        # closed narrowly instead: refuse only where the literal reading also
        # names a real directory, which is the only situation in which this hook
        # could confidently scan a DIFFERENT existing tree.
        #
        # Residual, stated rather than hidden: a quoted `~` whose literal form
        # does NOT exist makes bash's `cd` FAIL. After `&&` the push never runs,
        # so nothing is mis-scanned; after `;` the push runs in the original cwd
        # while this resolves to the expansion. Undetectable from the token, and
        # it costs a wrong-tree scan only for a mis-quoted command that also
        # ignored the cd's failure.
        # Resolved against the TRACKED shell cwd, not the hook process's.
        # `~/wt` read literally is a RELATIVE path, so `os.path.isdir(token)`
        # asked about `<hook process cwd>/~/wt` — a directory that has nothing
        # to do with where the command runs. After `cd sub && cd "~/wt"` bash is
        # in `<base>/sub/~/wt`, and checking the wrong base means the ambiguity
        # is missed exactly when a preceding relative `cd` has moved things.
        literal = token
        if not os.path.isabs(literal) and isinstance(base, str) and base:
            literal = os.path.join(base, token)
        if os.path.isdir(literal):
            return _CWD_UNRESOLVED
        token = expanded
    if any(ch in token for ch in _UNRESOLVABLE_CD_CHARS):
        return _CWD_UNRESOLVED
    return token


def _resolve(base, path, *, collapse: bool = True):
    """Resolve ``path`` against ``base``; either may be ``_CWD_UNRESOLVED``.

    An ABSOLUTE path recovers from an unresolved base — ``cd $W && git -C /wt
    push`` is fully determined by the ``-C``. A relative one cannot: with no
    known base there is nothing to join it to, and returning the bare relative
    string (the old behaviour) meant it was later handed to ``subprocess(cwd=)``
    and silently resolved against the HOOK's own directory.

    ``collapse`` decides whether ``..`` is folded LEXICALLY, and the two callers
    genuinely differ:

    * ``cd`` — bash resolves logically by default (``-L``), folding ``..``
      against the path text, so collapsing MATCHES the shell. Keep it.
    * ``git -C`` — git performs a real ``chdir``, so ``link/..`` lands in the
      parent of the link's TARGET, not the parent of the link. Collapsing it
      lexically names a different directory; with ``/base/link -> /other/child``
      git ends up in ``/other`` while normpath says ``/base``. The unfolded path
      is handed to ``subprocess(cwd=)``, which resolves it with the same
      filesystem semantics git would — so passing it through is not a
      shortcoming, it is the accurate answer.
    """
    if path is _CWD_UNRESOLVED:
        return _CWD_UNRESOLVED
    if os.path.isabs(path):
        return os.path.normpath(path) if collapse else path
    if base is _CWD_UNRESOLVED or not base:
        return _CWD_UNRESOLVED
    joined = os.path.join(base, path)
    return os.path.normpath(joined) if collapse else joined


def _emit(context: str) -> None:
    """Write the ONE output shape this hook is allowed to produce.

    additionalContext only: no ``permissionDecision``, so it composes with
    git_push_guard's ask/allow/deny on the same matcher and can never block.
    Both callers go through here so the contract has a single site.
    """
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": context,
            }
        },
        sys.stdout,
    )


def _effective_cwd(cmd: str, payload_cwd: str | None):
    """The directory the ``git push`` actually runs in.

    Honors a preceding ``cd <dir>`` segment and a ``git -C <dir>`` on the push
    itself (either overrides the payload cwd) — so a ``git -C <worktree> push``
    or ``cd <worktree> && git push`` is scanned against the repo actually being
    pushed, not the hook's payload cwd. Falls back to ``payload_cwd``.

    Returns ``_CWD_UNRESOLVED`` when the command text does not determine the
    directory; the caller reports that rather than scanning the wrong tree.
    """
    cwd = payload_cwd

    for seg in re.split(r"\|\||&&|[;|&]", cmd):
        try:
            toks = shlex.split(seg)
        except ValueError:
            continue
        if not toks:
            continue
        # A subshell or group SCOPES its cd, so the text cannot tell us what the
        # push's directory is. `toks[0]` would be "(" here, never "cd", so
        # without this the cd is invisible and the payload cwd survives — the
        # hook then scans a DIFFERENT repository and, if that one is clean, says
        # nothing. That is a false all-clear about a tree that was not pushed,
        # i.e. strictly worse than the bare-variable case this class began with.
        # Both sibling copies already model it (git_push_guard, and
        # review_enforcement_commit which is where this spelling comes from).
        #
        # This OVER-refuses, and deliberately so. Cross-model review noted that a
        # group which opens and closes before the push — `(cd /tmp); git push` —
        # leaves the directory fully knowable, so refusing there is a false
        # notice. Recovering it means deciding from text where the group ENDS.
        # An attempt to do that (`endswith(")")`) was caught by the parity lock
        # on three constructs where the closing character belonged to the PATH
        # rather than the group: `( cd /a/(x)`, `{ cd ${W}`, `{ cd /a/{x}`.
        #
        # MEASURED over 708 real pushes from this install: 3 carry a group
        # segment at all, and 0 carry a closed group holding a cd — the shape
        # the recovery would rescue. An open-set text predicate that fixes zero
        # measured commands, on an advisory whose current answer is already the
        # safe one, is not worth its failure modes. Refusing stays.
        if seg.lstrip()[:1] in ("(", "{"):
            cwd = _CWD_UNRESOLVED
            continue
        # A `cd <dir>` changes cwd for the following segments. A `cd` whose
        # target is not knowable poisons cwd rather than being skipped — being
        # skipped is what silently left the payload cwd in place and scanned a
        # different repository than the one being pushed.
        if toks[0] == "cd":
            # More than one operand is not a plain `cd <dir>`: either options
            # (`cd -P /x`) or an UNQUOTED path with whitespace, which shlex has
            # already split into separate tokens. Taking toks[1] there silently
            # resolved `cd /a b` to `/a` — a real directory that is not the one
            # bash would be in. Found by sweeping a generated construct space
            # against the sibling copies, which both refuse this.
            if len(toks) > 2:
                cwd = _CWD_UNRESOLVED
                continue
            cwd = _resolve(cwd, _cd_target(toks[1] if len(toks) == 2 else None, cwd))
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
                return (
                    _resolve(cwd, _cd_target(c_dir, cwd), collapse=False)
                    if c_dir is not None
                    else cwd
                )
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
        payload_cwd = payload.get("cwd") if isinstance(payload, dict) else None
        # Resolve the repo the push ACTUALLY runs in — honoring `git -C <dir>`
        # and a preceding `cd <dir>` — so we scan the branch being pushed, not
        # the payload cwd (Codex P2 on #1267).
        cwd = _effective_cwd(cmd, payload_cwd)
        # POSITIVE VALIDATION, deliberately here rather than in _effective_cwd
        # (which stays a pure text function so it can be unit-tested on synthetic
        # paths). Enumerating the constructs bash would expand can only ever catch
        # the ones somebody thought of; asking whether the answer is a real
        # directory closes the class categorically. A path that does not exist
        # means every git call below would fail and the scan would silently
        # produce nothing — which is the exact failure this hook is being fixed
        # for. Covers a falsy payload cwd with no `cd` to override it, too.
        if cwd is not _CWD_UNRESOLVED and (not cwd or not os.path.isdir(cwd)):
            cwd = _CWD_UNRESOLVED
        if cwd is _CWD_UNRESOLVED:
            # Say so. The scan did NOT run, and silence here reads as a clean
            # bill of health — the failure mode this hook exists to prevent.
            # Still advisory: additionalContext only, no decision, exit 0.
            _emit(
                "[Pre-push privacy review] ⚠️ Could not determine which repository "
                "this push targets — the command reaches it through a shell "
                "variable, a substitution, or a relative path with no known base, "
                "so the private-data scan DID NOT RUN. This is not an all-clear. "
                "Either re-run the push with a literal path so the scan can scope "
                "itself, or check the outgoing diff by hand before it lands."
            )
            return
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
        _emit(
            "[Pre-push privacy review] ⚠️ This push to the PUBLIC repo "
            "adds lines matching private-data patterns. Before it lands, confirm "
            "each is a generic placeholder (safe) or scrub the real value:\n"
            + "\n".join(lines[:_MAX_LINES])
        )
    except Exception:
        # An advisory must NEVER block a push — swallow everything, exit 0.
        return


if __name__ == "__main__":
    main()
