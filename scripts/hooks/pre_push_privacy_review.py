#!/usr/bin/env python3
"""PreToolUse: refuse this install's OWN private values in a PUBLIC push; advise on the rest.

TWO VERDICTS, split by confidence — the distinction is the whole design:

* **DENY** when the diff contains a value this install actually HAS (a
  release-fingerprint literal: hostname, home path, subnet, tailnet address; or
  a personal email), OR a value SHAPED like a credential whatever this install
  has seen (a vendor-prefixed key, a password inside a URL, a hardcoded
  assignment, or anything gitleaks recognises). Neither has a placeholder
  reading. Blocking here is the rare escalation the project's hook axioms allow.

  The two deny halves differ in one way that matters: the install-known half
  CANNOT fire on correct code, while a credential SHAPE legitimately appears in
  a test fixture for a credential detector. That is what the annotation path
  below exists for, and why it is required rather than a nicety.
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

SHAPE, not memory. The install-specific scanners only recognise values this box
HAS, so a key belonging to someone else — which is most of them — passed
untouched. The shape half reuses ``genesis.security.output_scanner``'s pattern
table through its public accessor (one credential-pattern table in the repo; a
second copy drifts, and the half that drifts is the half nobody watches), plus
one ``gitleaks`` run over the added lines for the JWT / PEM / high-entropy
classes no regex table reaches.

MEASURED, and it drove the scoping: the full table would have blocked 11 of 40
merged commits (27.5%), because it also serves outbound MESSAGES where a leaked
path matters. Scoped to shapes that are a credential by construction, that falls
to 7.5%, of which most are test fixtures the annotation clears.

ANNOTATION is required. Any of these ON the flagged line clears it — three
spellings because three tools are involved and none owns the others':
    # pragma: allowlist secret   (detect-secrets convention)
    # gitleaks:allow             (gitleaks convention)
    # genesis:verified-generic   (ours — a reserved domain, a CIDR constant)

WHOLE BRANCH, not the outgoing delta. Diffing against ``origin/<branch>`` scans
only unpushed commits, making everything already pushed invisible: the same
lines flag on a first push and go silent afterwards, so a re-push reports clean
on a branch that is not.

detect-secrets is still not used. Its PATH problem was fixed upstream
(``_resolve_detect_secrets`` is interpreter-relative now, so the old reason here
was stale), but the load-bearing one stands: it spawns one subprocess PER ADDED
LINE. gitleaks is one subprocess for the whole set.

Contract: emits ``hookSpecificOutput`` on stdout and ALWAYS exits 0 — either a
``permissionDecision: deny`` (known-value match) or an ``additionalContext``
advisory (generic match only), never both. It composes with git_push_guard on
the same Bash matcher; a deny from either hook is decisive.

FAILS OPEN on an unexpected error (silent exit 0), deliberately: a bug in this
hook must not brick every push on the install, and the CI leak-detector remains
the backstop for anything it misses. That is a real limit of the guarantee, not
an oversight — the deny path is deterministic string matching precisely so the
failure surface stays small.

Stdlib + the contribution sanitizer + output_scanner.
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
from collections import namedtuple
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


def _push_source_refs(cmd: str, cwd: str | None) -> list[str] | None:
    """EVERY local ref this push sends, or None for "just HEAD".

    A push is not always about the checked-out branch. ``git push origin
    otherbranch``, ``--all``, ``--mirror`` and ``--tags`` all publish refs that
    are not HEAD, and scanning HEAD's history for them inspects unrelated
    content — so a credential living only on a selected non-HEAD ref reached the
    public remote past a BLOCKING scanner with no signal at all.

    Returns ``[]`` for a pure deletion (nothing is sent, nothing to scan).
    Falls back to None (scan HEAD) whenever the argv cannot be read, which keeps
    the previous behaviour rather than failing open to "scan nothing".
    """
    try:
        argv = shlex.split(cmd)
    except ValueError:
        return None
    try:
        i = argv.index("push")
    except ValueError:
        return None
    rest = argv[i + 1 :]
    if any(a in ("--delete", "-d") for a in rest):
        return []

    wants_all = any(a in ("--all", "--mirror") for a in rest)
    wants_tags = any(a == "--tags" for a in rest)
    refs: list[str] = []
    if wants_all:
        out = _git(["for-each-ref", "--format=%(refname:short)", "refs/heads"], cwd)
        refs += [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    if wants_tags:
        out = _git(["for-each-ref", "--format=%(refname:short)", "refs/tags"], cwd)
        refs += [ln.strip() for ln in (out or "").splitlines() if ln.strip()]
    if refs:
        return refs

    positionals = [a for a in rest if not a.startswith("-")]
    # positionals: [remote] [refspec ...] — every refspec from the second onward.
    for spec in positionals[1:]:
        s = spec[1:] if spec.startswith("+") else spec
        src = s.split(":", 1)[0]
        if src:
            refs.append(src)
        else:
            return []  # ":dst" is a deletion
    return refs or None


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


def _scan_range(cwd: str | None, source_ref: str | None = None) -> tuple[str, str] | None:
    """``(base, tip)`` for one ref, or None when the base cannot be resolved."""
    tip = source_ref or "HEAD"
    base = _git(["merge-base", "origin/main", tip], cwd)
    return (base, tip) if base else None


def _outgoing_diff(cwd: str | None, source_ref: str | None = None) -> str:
    """The unified diff being pushed (local commits not yet on origin).

    ``source_ref`` is the LOCAL ref actually being sent; None means HEAD.
    """
    tip = source_ref or "HEAD"
    # Always the merge-base with main, never ``origin/<branch>``.
    #
    # Diffing against the remote branch scans only the commits not yet pushed,
    # which makes everything ALREADY pushed invisible. The same lines then flag
    # on a branch's first push and go silent on every push after — so a re-push
    # reports clean on a branch that is not, and the failure is silent in the
    # reassuring direction. Re-scanning the branch each push is pure regex over a
    # diff bounded by review size; being wrong costs more.
    base = _git(["merge-base", "origin/main", tip], cwd)
    if not base:
        return ""
    # `git log -p --no-merges`, NOT `git diff base..tip`.
    #
    # The net tree difference hides an add-then-remove: a commit that adds a
    # credential and a later commit that removes it cancel out, so the diff shows
    # nothing — while `git push` publishes BOTH commits and the blob stays
    # publicly recoverable. That is the ordinary shape of secret remediation, so
    # the case is common rather than exotic.
    #
    # It is also not hypothetical here: this very branch failed CI's leak
    # detector on exactly that pattern (a value added in one commit, replaced in
    # a later one, net-clean, still in history) — and CI caught it precisely
    # because it iterates commits while this hook did not. Same bug, opposite
    # outcome; the instrument that scanned every commit won.
    return _git(["log", "-p", "--no-merges", f"{base}..{tip}"], cwd) or ""


#: A line carrying any of these is a value a human has already judged generic.
#: Three spellings because three tools are involved and none owns the others' —
#: requiring ours alone would mean re-annotating lines already marked for
#: detect-secrets or gitleaks.
#:
#: The marker must be ON the flagged line; one on the line above does not count.
_ALLOW_MARKERS = (
    "pragma: allowlist secret",
    "gitleaks:allow",
    "genesis:verified-generic",
)

_GITLEAKS_BUDGET_S = 20

#: The subset of output_scanner's table this GATE acts on. Scoped deliberately:
#: that table also serves outbound MESSAGES, where a leaked path or a localhost
#: port matters. In repo code they are ordinary, and MEASURED across 40 merged
#: commits the broad table would have blocked 11 of them (27.5%) — 23 of the 35
#: hits were `internal_file_path` firing on `~/.genesis/`, which appears
#: legitimately throughout this repo. A gate that blocks a quarter of honest
#: pushes is turned off within a week, so this list holds only shapes that are a
#: CREDENTIAL by construction.
_PUSH_SHAPE_PATTERNS = frozenset(
    {
        "api_key_openai",
        "api_key_anthropic",
        "api_key_groq",
        "api_key_github",
        "api_key_aws",
        "url_credentials",
    }
)

#: Assignment shapes: a credential only when the RIGHT-HAND SIDE is a literal.
#: `KEY = os.environ[...]` is a reference and was 8 of the 11 false blocks in the
#: first measurement; `PASSWORD = "<value>"` is the real thing, and gitleaks does
#: NOT catch it (verified on both a low- and a high-entropy hardcoded value), so
#: dropping the class outright left a genuine gap rather than delegating it.
_ASSIGNMENT_SHAPE_PATTERNS = frozenset({"credential_assignment", "env_variable_secret"})

_RHS_REFERENCE_MARKERS = (
    "os.environ", "getenv", "environ[", "environ.get",
    "config", "settings", "secrets.", "vault", "keyring",
    "${", "$(", "%s", "{}", "...", "<", "none", "self.", "args.",
)

#: Shape/gitleaks findings, shaped so ``_render`` treats them like any other:
#: location plus a CATEGORY, never the matched text.
_ShapeFinding = namedtuple("_ShapeFinding", "file line scanner")


def _is_annotated(text: str) -> bool:
    """Whether this line carries an explicit "already judged generic" marker."""
    low = text.lower()
    return any(m in low for m in _ALLOW_MARKERS)


def _assignment_is_literal(text: str) -> bool:
    """Whether an assignment's RHS looks like an inline literal secret.

    Conservative in the FAIL-OPEN direction: anything that smells of indirection
    is treated as a reference and not refused. A missed hardcoded secret is still
    caught by CI and review; a gate that blocks every `KEY = os.environ[...]`
    line is a gate that gets disabled.
    """
    _, sep, rhs = text.partition("=")
    if not sep:
        return False
    rhs = rhs.strip()
    if rhs[:1] not in ("'", '"'):
        return False  # a bare identifier or call — not an inline literal

    # Isolate the ASSIGNED EXPRESSION before testing for reference markers.
    #
    # Scanning the whole remainder of the line let any trailing text disarm the
    # detector: `DATABASE_PASSWORD = "real-long-password"  # local config`
    # contains "config", so the line was classified as an indirect reference and
    # skipped — on exactly the low-entropy class the module's own comment records
    # gitleaks as NOT covering. A comment must not be able to vouch for the code
    # beside it.
    quote = rhs[0]
    end = rhs.find(quote, 1)
    if end == -1:
        return False  # unterminated literal — not something to judge
    value = rhs[1:end]
    remainder = rhs[end + 1 :].strip()

    # Markers are honoured only when they belong to the EXPRESSION — e.g.
    # `KEY = "x" + os.environ["Y"]` is still a reference — never when they merely
    # appear later on the line as prose. A remainder that starts a comment is
    # prose by construction.
    expression_tail = "" if remainder.startswith("#") else remainder
    if any(m in (value + " " + expression_tail).lower() for m in _RHS_REFERENCE_MARKERS):
        return False

    return len(value.strip()) >= 12 and not value.lower().startswith(
        ("xxx", "test", "dummy", "example")
    )


def _shape_findings(parsed) -> list:
    """Added lines SHAPED like a credential, whatever this install has seen.

    The install-specific scanners only recognise values this box HAS. This half
    is what catches a key belonging to someone else, or one that has never
    touched this machine — which is most of them.

    Reuses output_scanner's table through its public accessor: one
    credential-pattern table in the repo, because a second copy drifts and the
    half that drifts is the half nobody watches.
    """
    try:
        from genesis.security.output_scanner import iter_findings
    except Exception:
        return []  # fail OPEN: a missing scanner must not wedge the push
    out: list = []
    for file, line_no, text in parsed.added_lines:
        if _is_annotated(text):
            continue
        for name, _matched in iter_findings(text):
            if name in _ASSIGNMENT_SHAPE_PATTERNS:
                if not _assignment_is_literal(text):
                    continue
            elif name not in _PUSH_SHAPE_PATTERNS:
                continue
            out.append(_ShapeFinding(file, line_no, name))
    return out


def _gitleaks_range_findings(cwd: str | None, base: str, tip: str) -> list:
    """gitleaks over a COMMIT RANGE, which is what closes two gaps at once.

    ``--log-opts`` makes gitleaks walk every commit itself, so:

    * an add-then-remove within the branch is still seen (the earlier commit's
      blob is scanned), which a net-diff feed cannot do; and
    * BINARY blobs are scanned, which the line-oriented layers cannot see at all
      — ``parse_diff`` yields no added_lines for a binary file, so a credential
      inside a NUL-containing config or an archive was previously invisible to
      every layer and the blocking hook allowed it onto a public branch.

    Returns [] on any failure (absent binary, non-zero exit, unparseable report,
    timeout). This layer enriches; it must never be able to block a push by
    breaking.
    """
    import shutil as _sh
    import tempfile

    exe = _sh.which("gitleaks")
    if not exe or not base:
        return []
    out: list = []
    try:
        with tempfile.TemporaryDirectory(dir=os.path.expanduser("~/tmp")) as td:
            report = Path(td) / "report.json"
            subprocess.run(
                [exe, "detect", "--source", cwd or ".",
                 "--log-opts", f"{base}..{tip}",
                 "--report-format", "json", "--report-path", str(report),
                 "--no-banner", "--redact"],
                capture_output=True, text=True, timeout=_GITLEAKS_BUDGET_S,
            )
            if not report.is_file():
                return []
            data = json.loads(report.read_text() or "[]")
        for item in data if isinstance(data, list) else []:
            f = item.get("File") or "?"
            n = item.get("StartLine")
            rule = item.get("RuleID") or item.get("Description") or "gitleaks"
            out.append(_ShapeFinding(f, n if isinstance(n, int) else 0, f"gitleaks:{rule}"))
    except Exception:
        return []
    return out


def _gitleaks_findings(parsed) -> list:
    """gitleaks over the added lines, as ONE subprocess. [] when unavailable.

    One subprocess, not one per line — which is why this is gitleaks and not
    detect-secrets, whose scanner spawns a process per added line and would make
    a large diff a timeout. It is also where JWT / PEM / high-entropy coverage
    comes from, none of which a regex table reaches.

    Added lines go to a scratch file with a parallel index, so the line numbers
    gitleaks reports map back to real (file, line) pairs exactly.

    Every failure path returns []: absent binary, non-zero exit, unparseable
    report, timeout. This is the enrichment layer, and an enrichment layer must
    not be able to block a push by breaking.
    """
    import shutil as _sh
    import tempfile

    exe = _sh.which("gitleaks")
    if not exe or not parsed.added_lines:
        return []
    index: list = []
    lines: list[str] = []
    for file, line_no, text in parsed.added_lines:
        if _is_annotated(text):
            continue
        index.append((file, line_no))
        lines.append(text.replace("\n", " ").replace("\r", " "))
    if not lines:
        return []
    out: list = []
    try:
        with tempfile.TemporaryDirectory(dir=os.path.expanduser("~/tmp")) as td:
            src = Path(td) / "added.txt"
            src.write_text("\n".join(lines), encoding="utf-8", errors="replace")
            report = Path(td) / "report.json"
            subprocess.run(
                [exe, "detect", "--no-git", "--source", str(src),
                 "--report-format", "json", "--report-path", str(report),
                 "--no-banner", "--redact"],
                capture_output=True, text=True, timeout=_GITLEAKS_BUDGET_S,
            )
            if not report.is_file():
                return []
            data = json.loads(report.read_text() or "[]")
        for item in data if isinstance(data, list) else []:
            n = item.get("StartLine")
            rule = item.get("RuleID") or item.get("Description") or "gitleaks"
            if isinstance(n, int) and 1 <= n <= len(index):
                file, line_no = index[n - 1]
                out.append(_ShapeFinding(file, line_no, f"gitleaks:{rule}"))
    except Exception:
        return []
    return out


def _scan(
    diff_text: str,
    cwd: str | None = None,
    rng: tuple[str, str] | None = None,
) -> tuple[list, list]:
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
    text_at = {(f, n): s for f, n, s in parsed.added_lines}

    def _unannotated(findings: list) -> list:
        """Drop findings whose line carries an explicit generic marker.

        Applied to the install-specific scanners too, not only the shape ones.
        MEASURED: a single legitimate module produced 15 hits — a CIDR constant
        is the range a classifier is MADE of, and a reserved ``.invalid`` address
        is RFC 2606's whole purpose. Exempting only the new layers would leave
        the most common false positive unclearable, and a gate nobody can clear
        is a gate that gets switched off.
        """
        return [
            f
            for f in findings
            if not _is_annotated(text_at.get((f.file, f.line), ""))
        ]

    generic = _unannotated(list(sanitize._check_portability(parsed)))
    known = _unannotated(list(sanitize._check_emails(parsed)))
    fp_env = os.environ.get("GENESIS_RELEASE_FINGERPRINTS")
    fp = Path(fp_env) if fp_env else Path.home() / ".genesis" / "release-fingerprints.txt"
    if fp.is_file():
        known += _unannotated(sanitize._check_fingerprints(parsed, fp))

    # Credential SHAPES join the DENY half. A vendor-prefixed key or a password
    # in a URL has no placeholder reading either — the difference from the
    # install-known half is only that these can appear in a legitimate test
    # fixture, which is precisely what the annotation path above is for.
    known += _shape_findings(parsed)
    # Prefer the RANGE scan when the caller knows the range: gitleaks then walks
    # every commit itself and sees BINARY blobs, neither of which a line-oriented
    # feed can do. The added-lines feed remains for callers (and tests) that only
    # have a diff.
    if rng is not None:
        known += _gitleaks_range_findings(cwd, rng[0], rng[1])
    else:
        known += _gitleaks_findings(parsed)
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
        selected = _push_source_refs(cmd, cwd)
        if selected == []:
            return  # a deletion sends no content
        # None means "just HEAD" — the ordinary case, and the previous behaviour.
        refs: list[str | None] = list(selected) if selected else [None]
        source_ref = refs[0]
        if source_ref == "":
            return  # a deletion sends no content
        # Scan EVERY selected ref and merge the results. One ref failing to
        # resolve does not excuse the others, and a finding on any of them is a
        # finding for this push.
        diff_parts: list[str] = []
        ranges: list[tuple[str, str]] = []
        unresolved: list[str] = []
        for ref in refs:
            part = _outgoing_diff(cwd, ref)
            if part:
                diff_parts.append(part)
            else:
                unresolved.append(ref or "HEAD")
            # The range is an ENHANCEMENT (it lets gitleaks walk commits and see
            # binary blobs), never a precondition for scanning. A ref whose range
            # will not resolve but whose diff does must still be scanned by the
            # regex layers — gating on the range would turn a partial capability
            # loss into scanning nothing at all.
            r = _scan_range(cwd, ref)
            if r is not None:
                ranges.append(r)
        diff_text = "\n".join(d for d in diff_parts if d)
        if unresolved and not diff_text:
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
                            f"diff for {', '.join(unresolved)} — NOTHING was scanned "
                            "for private data on this push. Usually a shallow clone or "
                            "an origin/main that was never fetched. Treat this as "
                            "unchecked, not as clean."
                        ),
                    }
                },
                sys.stdout,
            )
            return
        if not diff_text:
            return
        known, generic = _scan(diff_text, cwd, ranges[0] if len(ranges) == 1 else None)
        if len(ranges) > 1:
            # Several refs: run the range scan for each, since --log-opts takes one.
            for r in ranges:
                known += _gitleaks_range_findings(cwd, r[0], r[1])
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
