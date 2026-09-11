#!/usr/bin/env python3
"""PreToolUse GATE: refuse a public push that adds private data or a credential.

BLOCKING. When a ``git push`` targets the public repo, this hook scans the whole
BRANCH for (a) this install's private data — install IPs, personal emails, local
release fingerprints — and (b) anything SHAPED like a credential, whether or not
this install has ever seen it. A surviving finding blocks the push (exit 2).

It used to be advisory, and the reason it no longer is: a branch pushed with no
PR receives no CI at all, so the CI ``leak-detector`` it deferred to never ran on
exactly the branches that needed it. An advisory that defers enforcement to a job
that does not execute is not a safety net.

SHAPE, not memory. The install-specific scanners only recognise values this box
has seen. The shape pass reuses ``genesis.security.output_scanner``'s pattern
table (GitHub/OpenAI/Anthropic/Groq/AWS/Google key shapes, credential
assignments, URL user:pass) and, when the binary is present, one ``gitleaks`` run
over the added lines — which is where JWT/PEM/high-entropy coverage comes from.
Reusing that table rather than copying it is deliberate: a second copy of a
credential-pattern table drifts, and the half that drifts is the half nobody
watches.

FAIL CLOSED ON A FINDING, FAIL OPEN ON AN ERROR. A scanner that crashes, times
out, or cannot resolve the repo must never wedge a push — that would stop all
work for a condition never established. A finding must never be sail-past-able.
Those are different failures and they get opposite defaults.

ANNOTATION IS REQUIRED, not a nicety. MEASURED on this install: a single
legitimate module produced 15 shape hits — ``_TAILNET_NET = ip_network(...)`` is
the range a classifier is MADE of, and a reserved ``.invalid`` address is RFC
2606's whole purpose. (This paragraph originally spelled that address out and
tripped the hook's own email scanner, which is the shortest possible argument for
the annotation path existing.) A blocking hook with no way to say "this one is generic" makes
such a branch unpushable and gets switched off within a week. Any of these on the
line clears it:
    # pragma: allowlist secret   (detect-secrets convention)
    # gitleaks:allow             (gitleaks convention)
    # genesis:verified-generic   (ours — for the portability/fingerprint class,
                                  which neither of the above covers)

WHOLE BRANCH, not the outgoing delta. Scanning only commits not yet on the remote
makes already-pushed content invisible: the same lines flag on a first push and
go silent on every push after, so a re-push reports clean on a branch that is not.

Detect-secrets is still not used here. Its PATH problem was fixed upstream
(``_resolve_detect_secrets`` is interpreter-relative now), but the other reason
stands and is the load-bearing one: it spawns one subprocess PER ADDED LINE.
gitleaks is one subprocess for the whole set.

Stdlib + the contribution sanitizer + output_scanner.
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
    return _norm_url(target_url) == _norm_url(origin_url)


def _outgoing_diff(cwd: str | None) -> str:
    """The WHOLE branch as a diff against its merge-base with main.

    Deliberately not ``origin/<branch>..HEAD``. That form scans only the commits
    not yet on the remote, which makes everything already pushed invisible: the
    same lines flag on a branch's first push and go silent on every push after,
    so a re-push reports clean on a branch that is not. The failure is silent in
    the reassuring direction, which is the worst kind for a privacy gate.

    The cost is re-scanning the branch each push. That is pure regex over a diff
    and the branch is bounded by review size, so it is cheap next to being wrong.
    """
    base = _git(["merge-base", "origin/main", "HEAD"], cwd)
    if not base:
        return ""
    return _git(["diff", f"{base}..HEAD"], cwd) or ""


#: A line carrying any of these is a value a human has already judged generic.
#: Three spellings because three tools are involved and none of them owns the
#: others' convention — requiring ours alone would mean re-annotating lines that
#: are already annotated for detect-secrets or gitleaks.
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
#: legitimately throughout this repo, and 8 more were assignment shapes matching
#: `SOMETHING_KEY = os.environ[...]`, a reference rather than a secret.
#:
#: A gate that blocks a quarter of honest pushes is turned off within a week, so
#: this list is restricted to shapes that are a CREDENTIAL by construction — a
#: vendor-prefixed key, or a password embedded in a URL. Entropy-based and
#: assignment-based detection is left to gitleaks, which judges the VALUE rather
#: than the surrounding text. Paths and private IPs remain the contribution
#: sanitizer's job, where they are already handled with install context.
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

#: Assignment-shaped patterns: a credential only when the RIGHT-HAND SIDE is a
#: literal. `ANTHROPIC_API_KEY = os.environ["..."]` is a reference and was 8 of
#: the 11 false blocks in the first measurement; `DATABASE_PASSWORD = "<value>"`
#: is the real thing, and gitleaks does NOT catch it (verified: it flags neither
#: a low- nor a high-entropy hardcoded assignment), so dropping the class
#: outright left a genuine gap rather than delegating it.
_ASSIGNMENT_SHAPE_PATTERNS = frozenset({"credential_assignment", "env_variable_secret"})

#: Spellings of "the value comes from somewhere else". A match here means the
#: line NAMES a credential rather than containing one.
_RHS_REFERENCE_MARKERS = (
    "os.environ", "getenv", "environ[", "environ.get",
    "config", "settings", "secrets.", "vault", "keyring",
    "${", "$(", "%s", "{}", "...", "<", "none", "self.", "args.",
)


def _assignment_is_literal(text: str) -> bool:
    """Whether an assignment's RHS looks like an inline literal secret.

    Conservative in the FAIL-OPEN direction on purpose: anything that smells of
    indirection is treated as a reference and NOT blocked. A missed hardcoded
    secret is still caught by CI and by review; a gate that blocks every
    `KEY = os.environ[...]` line is a gate that gets disabled.
    """
    _, sep, rhs = text.partition("=")
    if not sep:
        return False
    rhs = rhs.strip()
    low = rhs.lower()
    if any(m in low for m in _RHS_REFERENCE_MARKERS):
        return False
    if rhs[:1] not in ("'", '"'):
        return False  # a bare identifier or call — not an inline literal
    value = rhs.strip("'\"").strip()
    # Too short to be a credential, or an obvious placeholder.
    return len(value) >= 12 and not value.lower().startswith(("xxx", "test", "dummy", "example"))


def _is_annotated(text: str) -> bool:
    """Whether this line carries an explicit "already judged generic" marker."""
    low = text.lower()
    return any(m in low for m in _ALLOW_MARKERS)


def _shape_findings(parsed) -> list[tuple[str, int, str]]:
    """(file, line, message) for added lines SHAPED like a credential.

    Independent of what this install has seen — that is the whole point. Reuses
    output_scanner's table via its public accessor so there is exactly one
    credential-pattern table in the repo.

    The matched TEXT is never included in the message. This output reaches a
    transcript and a terminal; echoing the secret to report the secret would
    leak it to one more place.
    """
    try:
        from genesis.security.output_scanner import iter_findings
    except Exception:
        return []  # fail OPEN: a missing scanner must not wedge the push
    out: list[tuple[str, int, str]] = []
    for file, line_no, text in parsed.added_lines:
        if _is_annotated(text):
            continue
        for name, _matched in iter_findings(text):
            if name in _ASSIGNMENT_SHAPE_PATTERNS:
                if not _assignment_is_literal(text):
                    continue
            elif name not in _PUSH_SHAPE_PATTERNS:
                continue
            out.append((file, line_no, f"Credential-shaped value ({name})"))
    return out


def _gitleaks_findings(parsed) -> list[tuple[str, int, str]]:
    """gitleaks over the added lines, as ONE subprocess. [] when unavailable.

    Added lines are written to a scratch file with a parallel index, so the line
    numbers gitleaks reports map back to real (file, line) pairs exactly rather
    than approximately.

    Every failure path returns [] — absent binary, non-zero exit, unparseable
    report, timeout. This is the enrichment layer; the regex passes above are the
    floor, and an enrichment layer must not be able to block a push by breaking.
    """
    import shutil
    import tempfile

    exe = shutil.which("gitleaks")
    if not exe or not parsed.added_lines:
        return []
    index: list[tuple[str, int]] = []
    lines: list[str] = []
    for file, line_no, text in parsed.added_lines:
        if _is_annotated(text):
            continue
        index.append((file, line_no))
        lines.append(text.replace("\n", " ").replace("\r", " "))
    if not lines:
        return []
    out: list[tuple[str, int, str]] = []
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
                out.append((file, line_no, f"Credential-shaped value ({rule})"))
    except Exception:
        return []
    return out


def _scan(diff_text: str) -> list[tuple[str, int, str]]:
    """Every surviving finding as (file, line, message), annotations removed.

    Three layers, cheapest first: this install's KNOWN values (the contribution
    sanitizer's regex scanners), then anything credential-SHAPED regardless of
    whether this box has seen it, then gitleaks for the entropy/JWT/PEM classes
    the regexes do not cover.

    Annotation is applied to ALL of them, including the install-specific
    scanners. Those produce the hits most likely to be legitimate — a CIDR
    constant in a network classifier, a reserved-domain fixture — so exempting
    only the new layers would leave the common false positive unclearable.
    """
    from genesis.contribution import sanitize

    parsed = sanitize.parse_diff(diff_text)
    # (file, line) -> added text, so a finding can be tested for its annotation.
    text_at = {(f, n): s for f, n, s in parsed.added_lines}

    sanitizer_findings = list(sanitize._check_portability(parsed))
    sanitizer_findings += sanitize._check_emails(parsed)
    fp_env = os.environ.get("GENESIS_RELEASE_FINGERPRINTS")
    fp = Path(fp_env) if fp_env else Path.home() / ".genesis" / "release-fingerprints.txt"
    if fp.is_file():
        sanitizer_findings += sanitize._check_fingerprints(parsed, fp)

    out: list[tuple[str, int, str]] = []
    for f in sanitizer_findings:
        line_text = text_at.get((f.file, f.line), "")
        if line_text and _is_annotated(line_text):
            continue
        out.append((f.file, f.line, f.message))

    out += _shape_findings(parsed)
    out += _gitleaks_findings(parsed)
    return out


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
        diff_text = _outgoing_diff(cwd)
        if not diff_text:
            return
        findings = _scan(diff_text)
        if not findings:
            return

        seen: set = set()
        lines: list[str] = []
        for file, line_no, message in findings:
            key = (file, line_no, message)
            if key in seen:
                continue
            seen.add(key)
            lines.append(f"  {file or '?'}:{line_no or '?'}  {message}")

        shown = lines[:_MAX_LINES]
        extra = len(lines) - len(shown)
        # Say how many were omitted rather than cutting silently — a bounded list
        # that looks complete is how a reader concludes they have seen it all.
        tail = f"\n  ... and {extra} more" if extra > 0 else ""

        print(
            "BLOCKED: this push to the PUBLIC repo adds lines carrying private "
            "data or a credential-shaped value.\n"
            + "\n".join(shown)
            + tail
            + "\n\nScrub the real value, or — if it is genuinely generic — mark the "
            "line and push again:\n"
            "    # pragma: allowlist secret   (a key-shaped placeholder)\n"
            "    # gitleaks:allow             (same, gitleaks' spelling)\n"
            "    # genesis:verified-generic   (a reserved domain, a CIDR constant, "
            "a documented example)\n"
            "Annotate only what you have actually checked: this is the last "
            "automated look before the value is public and irrevocable.",
            file=sys.stderr,
        )
        sys.exit(2)
    except SystemExit:
        raise  # the block above — never swallowed by the fail-open below
    except Exception:
        # FAIL OPEN on an ERROR, never on a finding. A scanner that crashes or a
        # repo that will not resolve must not wedge every push on the box; that
        # would stop all work for a condition never established. The finding path
        # exits 2 above and is deliberately outside this.
        return


if __name__ == "__main__":
    main()
