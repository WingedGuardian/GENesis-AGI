#!/usr/bin/env python3
"""PreToolUse gate: private-data leaks in an outgoing PUBLIC push.

TWO VERDICTS, split by how confident the match is.

ADVISORY (the default, and what this hook was originally): CLASS findings —
RFC1918-shaped addresses, ``/home/<user>`` shapes, personal emails. These are
regexes over generic patterns and they have real false positives; this repo's
own test fixtures carry a dozen legitimate ones. They inject a review prompt
into the model's context and never block.

BLOCKING: a FINGERPRINT match — an entry from ``~/.genesis/release-fingerprints.txt``,
the install's own curated list of its private values. The sanitizer already
classifies such a hit as ``Severity.BLOCK``; this change makes the push hook act
on the severity that was already assigned, rather than inventing a new judgement.

Be precise about what "fingerprint" means, because an earlier draft of this
docstring said "exact literals" and that is FALSE: ``sanitize._check_fingerprints``
compiles each line as a REGEX, falling back to ``re.escape`` only for a line that
is not valid regex — and on this install 4 of 7 entries carry metacharacters. So
an entry is as precise as whoever wrote it: an escaped one is exact, an unescaped
dot is a wildcard. The reason this tier blocks and the class tier does not is
therefore NOT "zero false positives"; it is that these patterns are curated
per-install to name that install's own values, while the class patterns are
generic shapes this repo's own fixtures legitimately contain by the dozen.

WHY THIS ONE BLOCKS, measured 2026-09-07. The advisory fired correctly on a real
push and the push proceeded in the same tool call, because that is what an
advisory does — so the warning arrived AFTER the irreversible act it described.
A published fingerprint cannot be fixed forward: ``private_pattern_scan`` in CI
deliberately scans every commit's ADDED lines rather than the net diff (a value
added then removed still lives in published history), and force-push is banned
here, so the only remediation was abandoning the branch, opening a replacement
PR, and deleting the old ref. CI is the right place to enforce a merge; it is the
wrong place to prevent a publication, because by the time it speaks the value is
already public.

This does not cost a background session anything: ``git_push_guard`` already
hard-denies ``git push`` in a Genesis-dispatched session (no human to approve),
so the only sessions that reach this gate are foreground ones where someone can
act on it.

Fail direction is OPEN, deliberately: any error, timeout, or unreadable
fingerprint file leaves the push alone. A fresh clone has no fingerprint file at
all, and a hook bug that blocked every push would be far worse than the leak it
prevents — CI still refuses the merge either way.

Reuses the contribution sanitizer's cheap REGEX scanners (``parse_diff`` +
``_check_portability`` / ``_check_emails`` / ``_check_fingerprints``) — NOT the
full ``scan_diff``, whose detect-secrets floor is fail-CLOSED (a false "missing
binary" finding on every push, since the venv bin isn't on the hook's PATH) and
whose secret scanners spawn one subprocess per added line (latency / timeout
kill on large diffs).

Contract: on the advisory path it emits ONLY
``hookSpecificOutput.additionalContext`` on stdout and exits 0, carrying no
``permissionDecision`` — so it composes with git_push_guard's ask/allow/deny on
the same Bash matcher (each hook is a separate process; additionalContext is
concatenated, order-independent). On the blocking path it exits 2 with the reason
on stderr, the same mechanism every other blocking gate in this repo uses; the
wrapper ``exec``s the interpreter, so the code propagates. Any error → silent
exit 0.

The escape is ``# privacy-override`` as a trailing comment on the push command,
matched by the shared ``shell_parse.has_trailing_override`` (never a local
regex), and registered in ``_KNOWN_SIGILS`` so it does not silently end the
leading sigil run for anything written after it. It is for the deliberate case —
updating the fingerprint file itself, or a fingerprint entry so broad it matches
generic text — and it announces itself in context rather than passing silently.

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
from shell_parse import analyze, has_trailing_override  # noqa: E402

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


def _resolve_target(remote: str, cwd: str | None) -> str:
    """``"public"`` | ``"other"`` | ``"unknown"`` for the push destination.

    The two-value version of this returned True on "unresolvable" — correct while
    the verdict was advisory, because over-informing is free. It is wrong for a
    gate: an unreadable remote would be reported to the operator as a PUBLIC push
    and their only way forward would be a sigil whose message says "you are
    deliberately publishing this value", which is false for someone not
    publishing at all. So uncertainty still ADVISES (a warning nobody needs costs
    nothing) and only a confident match BLOCKS.
    """
    if _targets_public_repo(remote, cwd):
        return "public" if _target_resolved(remote, cwd) else "unknown"
    return "other"


def _target_resolved(remote: str, cwd: str | None) -> bool:
    """True when both the target and origin URLs were actually readable."""
    origin_url = _git(["remote", "get-url", "--push", "origin"], cwd)
    if not origin_url:
        return False
    if remote == "":
        tracked = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{push}"], cwd)
        name = tracked.split("/", 1)[0] if tracked else "origin"
        return bool(_git(["remote", "get-url", "--push", name], cwd))
    if "://" in remote or re.match(r"^[^/\s]+@[^/\s]+:", remote):
        return True  # a literal URL is self-resolving
    return bool(_git(["remote", "get-url", "--push", remote], cwd))


def _targets_public_repo(remote: str, cwd: str | None) -> bool:
    """True if the push destination is origin (public), or is unresolvable.

    Advisory bias, deliberately retained HERE: scan on origin AND on anything we
    cannot confidently resolve to a NON-origin remote; skip only when the target
    clearly resolves to a different remote (e.g. a private fork), where real
    install IPs are allowed. `_resolve_target` is what separates the confident
    case from the uncertain one for the BLOCKING decision.
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
    """The added lines this branch publishes — PER COMMIT, not the net difference.

    This is the same model `scripts/ci/leak_scan_added_lines.py` uses, and the
    reason is this hook's whole justification: a value added in one commit and
    removed in the next leaves a CLEAN net diff while shipping in published
    history forever. Scanning `git diff base..HEAD` would therefore wave through
    precisely the scenario the block message tells the operator is unfixable.
    MEASURED against the real hook: an add-then-scrub branch scored 0 findings on
    the net diff and 1 on the per-commit walk.

    The base is the merge-base with origin/main, again matching CI — anchoring on
    `origin/<branch>` instead would mean that after the first push the scan only
    ever sees the newest commits, so a leak already pushed to the branch stops
    being reported the moment it is on the remote.
    """
    base = _git(["merge-base", "origin/main", "HEAD"], cwd)
    if not base:
        return ""
    return _git(["log", "-p", "--no-merges", "--format=", f"{base}..HEAD"], cwd) or ""


def _scan(diff_text: str) -> tuple[list, list]:
    """Run only the cheap regex scanners from the contribution sanitizer.

    Returns ``(class_findings, fingerprint_findings)`` as two lists, kept apart
    STRUCTURALLY — by which scanner produced them — rather than by matching on a
    finding's message text, which would silently reclassify the moment the
    sanitizer rewords itself. The split is what decides advisory vs block, so it
    has to be the sturdier of the two.
    """
    from genesis.contribution import sanitize

    parsed = sanitize.parse_diff(diff_text)
    class_findings = list(sanitize._check_portability(parsed))
    class_findings += sanitize._check_emails(parsed)
    fingerprint_findings: list = []
    fp_env = os.environ.get("GENESIS_RELEASE_FINGERPRINTS")
    fp = Path(fp_env) if fp_env else Path.home() / ".genesis" / "release-fingerprints.txt"
    if fp.is_file():
        fingerprint_findings = list(sanitize._check_fingerprints(parsed, fp))
    return class_findings, fingerprint_findings


def _render(findings: list) -> list[str]:
    """One deduplicated ``file:line  message`` line per finding.

    On the BLOCK path this never carries content: ``_check_fingerprints`` sets a
    constant message, and the raw line lives in ``Finding.detail``, which is not
    read here. Scope the claim to that path and no further — the email scanner
    embeds the address in its own message, so the ADVISORY path does print it.
    That is pre-existing behaviour and useful there (the author needs to see which
    address), but a blanket "never the matched CONTENT" would have been a false
    sentence sitting one line above the call that disproves it.
    """
    seen: set = set()
    lines: list[str] = []
    for finding in findings:
        key = (finding.file, finding.line, finding.message)
        if key in seen:
            continue
        seen.add(key)
        lines.append(f"  {finding.file or '?'}:{finding.line or '?'}  {finding.message}")
    return lines


def _push_segments(cmd: str) -> list:
    """Every ``git push`` SEGMENT, via the shared parser the sibling guards use.

    NOT the local ``_push_remote`` splitter, which is kept only for the remote
    NAME. That splitter never splits on a NEWLINE, so this repo's own house
    idiom — `git add -A && git commit -m x` on one line, `git push` on the next —
    parsed as a single non-push segment and skipped the scan entirely. MEASURED:
    4 of 5 ordinary spellings (a preceding `git status`, a preceding `cd`, a
    multi-line commit-then-push, an absolute `/usr/bin/git`) returned "not a
    push" while the shared parser found the push in all 5.

    Under an advisory that miss cost a missing warning. Under a gate it is a
    fail-OPEN on a publication guard — and `git_push_guard` runs on the same tool
    call and DOES parse those spellings, so the operator gets its approval prompt,
    which reads as "this push was reviewed", with no privacy verdict behind it.
    """
    return [s for s in analyze(cmd) if s.exe == "git" and "push" in s.argv]


def main() -> None:
    try:
        payload = read_payload()
        cmd = field(payload, "command")
        if not cmd:
            return
        push_segs = _push_segments(cmd)
        if not push_segs:
            return  # not a git push
        # The remote NAME still comes from the local parser; when it cannot read
        # one, "" means "the branch's default push remote", which resolves the
        # same way a bare `git push` does.
        remote = _push_remote(cmd)
        if remote is None:
            remote = ""
        # All git calls below share ONE wall-clock budget (see _GIT_BUDGET_S) so
        # the chain can never approach the hook's CC timeout (a timeout = block).
        global _deadline
        _deadline = time.monotonic() + _GIT_BUDGET_S
        payload_cwd = payload.get("cwd") if isinstance(payload, dict) else None
        # Resolve the repo the push ACTUALLY runs in — honoring `git -C <dir>`
        # and a preceding `cd <dir>` — so we scan the branch being pushed, not
        # the payload cwd (Codex P2 on #1267).
        cwd = _effective_cwd(cmd, payload_cwd)
        target = _resolve_target(remote, cwd)
        if target == "other":
            return  # private-fork / non-origin push — real IPs allowed there
        diff_text = _outgoing_diff(cwd)
        if not diff_text:
            return
        # The scan runs inside the SAME wall-clock budget as the git calls. It is
        # regex work over a user-editable pattern file, and a PreToolUse hook that
        # times out is treated as a BLOCK — an external kill no `try` can catch.
        # MEASURED on a catastrophic-backtracking pattern: 0.20s at 20 input
        # characters, 7.11s at 26, 28.61s at 28, doubling per character, against
        # this hook's 30s settings.json timeout. One unlucky fingerprint entry
        # would otherwise make the repo unpushable — inverting the fail-OPEN this
        # hook promises, in the one direction it must never fail.
        if _deadline is not None and time.monotonic() >= _deadline:
            return
        class_findings, fingerprint_findings = _scan(diff_text)
        if _deadline is not None and time.monotonic() >= _deadline:
            return  # the scan itself overran — fail OPEN, as every git overrun does
        if not class_findings and not fingerprint_findings:
            return

        # Scoped to the PUSH segment, like 14 of the 15 sibling call sites.
        # `_has_trailing_override` breaks at the first unquoted `#` in whatever it
        # is given and its token scan crosses newlines, so passing the whole
        # command made "trailing comment" mean "any comment anywhere". MEASURED:
        # a comment written for a following `rm`, and one on a later `echo` line,
        # both waived the block on a push the author never meant to override.
        # (Quoted mentions were already safe — the parser's quote tracking is
        # sound, verified against `bash -c '… # privacy-override'` and
        # `echo '# privacy-override'`, both correctly refused.)
        overridden = any(has_trailing_override(s.raw, "privacy-override") for s in push_segs)
        # Only a CONFIDENTLY public target blocks; `unknown` falls through to the
        # advisory below (see _resolve_target).
        if fingerprint_findings and not overridden and target == "public":
            sys.stderr.write(
                "BLOCKED: this push to the PUBLIC repo adds "
                f"{len(fingerprint_findings)} line(s) matching an entry in this "
                "install's release-fingerprints file. Locations (content withheld):\n"
                + "\n".join(_render(fingerprint_findings)[:_MAX_LINES])
                + "\n\nThis is blocked rather than flagged because a push cannot be "
                "taken back. CI scans every commit's added lines, not the net diff, so "
                "a later scrub commit does NOT clear it — remediation once published is "
                "abandoning the branch and opening a replacement PR.\n"
                "Fix it here: replace the value with a synthetic stand-in (a test "
                "fixture almost never depends on the real one), then re-stage and "
                "amend. If the value is genuinely generic and the fingerprint entry is "
                "too broad, correct the fingerprint file. If you are deliberately "
                "pushing this value — updating the fingerprints themselves is the real "
                "case — append '# privacy-override' to the push command; it is a "
                "conscious, announced act, not a way around the check.\n"
            )
            sys.exit(2)

        header = "[Pre-push privacy review] "
        if overridden and fingerprint_findings:
            header += (
                "⚠️ OVERRIDDEN: '# privacy-override' waived a block on "
                f"{len(fingerprint_findings)} exact fingerprint match(es). "
                "This push publishes them. "
            )
        context = (
            header + "This push to the PUBLIC repo adds lines matching private-data "
            "patterns. Before it lands, confirm each is a generic placeholder "
            "(safe) or scrub the real value:\n"
            + "\n".join(_render(fingerprint_findings + class_findings)[:_MAX_LINES])
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
    except SystemExit:
        # SystemExit derives from BaseException, so `except Exception` below would
        # not catch it anyway. This clause is documentation, not a repair: it makes
        # the deliberate block visibly exempt from the swallow-everything guard, so
        # nobody later "tidies up" by widening that guard to BaseException.
        raise
    except Exception:
        # Any OTHER failure leaves the push alone. A hook bug that blocked every
        # push would cost more than the leak it prevents, and CI still refuses
        # the merge.
        return


if __name__ == "__main__":
    main()
