"""Phase 6 contribution — diff sanitization library.

Fail-closed gate for community contributions. Scans a commit diff
against multiple detectors; ANY finding blocks the contribution.

Scanners (all run when inputs are available):

1. Forbidden paths — diff touches files on the CONTRIBUTION_FORBIDDEN
   list (USER.md, secrets.env, research-profiles/**, docs/plans/**,
   etc.) → BLOCK.
2. Binary files — diff contains "Binary files ... differ" → BLOCK
   (we refuse to contribute binaries via this path).
3. Size cap — diff exceeds MAX_DIFF_BYTES → BLOCK (prevents huge
   drive-by PRs).
4. Secrets via `detect-secrets scan --string` on added lines → BLOCK
   per finding.
5. Secrets via `gitleaks detect` if binary is on PATH → BLOCK
   (optional second-layer, MVP-advisory).
6. Portability patterns — IPs, /home/<user>/ paths, hardcoded
   usernames, known private hostnames → BLOCK.
7. Fingerprint scan — user-defined strings from
   ~/.genesis/release-fingerprints.txt → BLOCK.
8. Personal email domains outside the allowlist → BLOCK.

The sanitizer does NOT mutate the diff. Fail-closed means: any
BLOCK finding returns `ok=False` and the pipeline stops. The PR
body receives the scanners_run list + the finding list for the
user's "why was this rejected?" explanation.
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from .findings import Finding, FindingKind, SanitizerResult, Severity

# Hard cap on diff size. Community contributions should be small
# focused fixes. Larger diffs are almost always refactors or
# feature additions — out of MVP scope.
MAX_DIFF_BYTES = 256 * 1024  # 256 KB

# Default CONTRIBUTION_FORBIDDEN paths. Authoritative source is
# config/protected_paths.yaml; this embedded list is a safety floor
# in case the yaml is missing or unreadable.
DEFAULT_FORBIDDEN_GLOBS: tuple[str, ...] = (
    "src/genesis/identity/USER.md",
    "src/genesis/identity/USER_KNOWLEDGE.md",
    "src/genesis/identity/*.md",
    "secrets.env",
    "*/secrets.env",
    ".env",
    # Sanitizer's own secret-scan rules — a contribution must not weaken the gate
    # that scans it (else a merged edit silently disables detection downstream).
    ".gitleaks.toml",
    "config/research-profiles/*",
    "config/research-profiles/**",
    "config/external-modules/*",
    "config/external-modules/**",
    "config/model_routing.yaml",
    "docs/plans/**",
    "docs/history/**",
    "docs/gtm/**",
    "docs/superpowers/**",
    "src/genesis/skills/voice-master/references/exemplars/*",
    "src/genesis/skills/voice-master/references/voice-dimensions.md",
    # Match any dotfile that mentions secrets or credentials
    "**/credentials*",
    "**/.aws/**",
    "**/.ssh/**",
    "~/.genesis/release-fingerprints.txt",
    # Local-only module paths (belt-and-suspenders with gitignore scanner)
    "src/genesis/modules/automaton_supervisor/**",
    "config/modules/automaton-supervisor.yaml",
)

# Portability patterns — install-specific SHAPES that should never appear in a
# public-facing contribution. These are generic CLASSES (all RFC1918 space, the
# IPv6 ULA space per RFC 4193, and /home/<user> path shapes) — deliberately NOT
# this install's literal values. A public scanner must never name what it
# redacts, so exact install values live ONLY in the local fingerprint file
# (see _check_fingerprints) and the CI secret, never in tracked source.
#
# The network regexes are kept byte-identical to scripts/check_portability.sh so
# there is a single source of truth for the class vocabulary; a cross-surface
# drift test asserts these, the commit-msg hook, and check_portability.sh all
# agree. Use [0-9] (not \d) so every pattern is valid under BOTH Python re and
# grep -E — the commit-msg hook consumes the same class vocabulary via grep -E.
#
# These are a strict superset of the former install-specific literals: each old
# literal is a member of its class (a specific /16 within RFC1918 10/8, a
# specific /24 within 192.168/16, specific ULA prefixes within RFC 4193 space, a
# specific home path within /home/<user>), so no prior coverage is lost. Labels
# use CIDR shorthand (no full dotted-quad) so this file does not self-match the
# class scan.
_PORTABILITY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"/home/[a-z_][a-z0-9_-]*/genesis", "absolute /home/<user>/genesis path"),
    (r"/home/[a-z_][a-z0-9_-]*/\.[A-Za-z]", "absolute path to a user dotfile"),
    (r"-home-[a-z0-9-]+-genesis", "CC project-dir slug"),
    (r"\b10\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\b", "private 10/8 address"),
    (
        r"\b172\.(1[6-9]|2[0-9]|3[01])\.[0-9]{1,3}\.[0-9]{1,3}\b",
        "private 172.16/12 address",
    ),
    (r"\b192\.168\.[0-9]{1,3}\.[0-9]{1,3}\b", "private 192.168/16 address"),
    (
        r"\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}\b",
        "CGNAT 100.64/10 address",
    ),
    (r"\b[fF][cdCD][0-9a-fA-F]{2}:", "IPv6 ULA prefix (RFC 4193)"),
)

# Email regex — loosely based on the release script's pattern.
_EMAIL_RE = re.compile(
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"
)

# Allowlist of email patterns that are safe to include. Aligned with
# the release script's phase 8b generic email scan.
_EMAIL_ALLOWLIST_RE = re.compile(
    r"("
    r"(^|[^A-Za-z0-9._+\-])(noreply|no-reply|pr-bot)@|"
    r"backup@genesis\.local|"
    r"feedback@anthropic\.com|"
    r"support@anthropic\.com|"
    r"@(example|example\.com|example\.org|localhost|test|invalid)\b|"
    r"@claude\.com\b|"
    r"@(github|gitlab|sentry|grafana|slack|discord)\.com\b|"
    r"user@\d+\.service|"
    r"@genesis\.local"
    r")"
)

# Compiled at import time for speed.
_PORTABILITY_COMPILED = [(re.compile(p), label) for p, label in _PORTABILITY_PATTERNS]


@dataclass
class _ParsedDiff:
    """Lightweight parse of a unified diff."""

    file_paths: list[str]      # distinct files touched
    added_lines: list[tuple[str, int, str]]  # (file, line_no, text) for `+` lines
    is_binary: bool
    size_bytes: int


def _normalize_diff_path(raw: str) -> str:
    """Normalize a `+++ b/path` or `+++ "b/path"` header value to a plain path.

    Git emits C-style quoted paths when the filename contains special
    chars (spaces, non-ASCII, control bytes) unless `core.quotepath=false`.
    An unquoted `b/foo` becomes `foo`; a quoted `"b/f\303\266o"` becomes
    `föo`. Failing to normalize lets forbidden paths sneak past the
    glob matcher (P1-2 from the code review).
    """
    path = raw.strip()
    if path.startswith('"') and path.endswith('"') and len(path) >= 2:
        inner = path[1:-1]
        # C-style escape decode — git uses unicode_escape over UTF-8 bytes
        try:
            path = (
                inner.encode("latin-1", "backslashreplace")
                .decode("unicode_escape")
                .encode("latin-1", "backslashreplace")
                .decode("utf-8", "replace")
            )
        except (UnicodeDecodeError, UnicodeEncodeError):
            path = inner  # best-effort; downstream glob will still likely fail-closed
    if path.startswith("a/") or path.startswith("b/"):
        path = path[2:]
    return path


_DIFF_GIT_RE = re.compile(r"^diff --git (.+?) (.+?)$")


def _extract_diff_git_paths(header: str) -> tuple[str | None, str | None]:
    """Extract (a-path, b-path) from a `diff --git` line.

    Handles both quoted (`"a/..."`) and unquoted forms. Returns
    whatever it can parse; None for unparseable halves.
    """
    m = _DIFF_GIT_RE.match(header)
    if not m:
        return None, None
    a_raw, b_raw = m.group(1), m.group(2)
    a = _normalize_diff_path(a_raw) if a_raw else None
    b = _normalize_diff_path(b_raw) if b_raw else None
    return a, b


def parse_diff(diff_text: str) -> _ParsedDiff:
    """Parse a unified diff into per-file added lines.

    Tracks added lines (`+...`) AND all file paths touched by the
    commit — including metadata-only changes like renames
    (`rename from`/`rename to`), mode flips (`old mode`/`new mode`),
    and new-file-mode stubs. A pure rename of a forbidden file
    emits NO `+++` header, so relying on `+++` alone let
    `src/genesis/identity/USER.md` get renamed into `docs/` without
    tripping the forbidden-path check (codex review P1 finding).

    Removed lines are what's leaving the codebase, not what the
    contribution ships — they're not scanned for content. Binary
    patches are flagged separately.
    """
    files: list[str] = []
    added: list[tuple[str, int, str]] = []
    current_file: str | None = None
    line_no = 0
    is_binary = False

    def _add_file(path: str | None) -> None:
        if path and path != "/dev/null" and path not in files:
            files.append(path)

    for raw in diff_text.splitlines():
        # `diff --git a/foo b/bar` — seen BEFORE +++/--- headers. This
        # is the only header present for rename-only or mode-only commits.
        if raw.startswith("diff --git "):
            a, b = _extract_diff_git_paths(raw)
            _add_file(a)
            _add_file(b)
            continue
        # Rename markers — git emits both when a file is renamed.
        if raw.startswith("rename from "):
            _add_file(_normalize_diff_path(raw[len("rename from "):].strip()))
            continue
        if raw.startswith("rename to "):
            _add_file(_normalize_diff_path(raw[len("rename to "):].strip()))
            continue
        # Mode changes — file paths already captured via `diff --git` line
        # above, but record for completeness (some diff-format variants
        # omit diff --git when feeding into patch tools).
        if raw.startswith("old mode ") or raw.startswith("new mode "):
            continue  # no path info here; captured upstream
        if raw.startswith("+++ "):
            # +++ b/path/to/file  OR  +++ /dev/null  OR  +++ "b/path with space"
            path_raw = raw[4:].strip()
            if path_raw == "/dev/null":
                current_file = None
                line_no = 0
                continue
            path = _normalize_diff_path(path_raw)
            current_file = path
            _add_file(path)
            line_no = 0
            continue
        if raw.startswith("--- "):
            continue
        if raw.startswith("Binary files") and "differ" in raw:
            is_binary = True
            continue
        if raw.startswith("@@"):
            # @@ -a,b +c,d @@
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw)
            if m:
                line_no = int(m.group(1)) - 1
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            line_no += 1
            if current_file is not None:
                added.append((current_file, line_no, raw[1:]))
        elif raw.startswith(" ") or raw.startswith("-"):
            if raw.startswith(" "):
                line_no += 1

    return _ParsedDiff(
        file_paths=files,
        added_lines=added,
        is_binary=is_binary,
        size_bytes=len(diff_text.encode("utf-8")),
    )


def _load_forbidden_globs(config_path: Path | None) -> tuple[str, ...]:
    """Load CONTRIBUTION_FORBIDDEN globs from protected_paths.yaml if present.

    Falls back to DEFAULT_FORBIDDEN_GLOBS on any error. The yaml file
    may not define the section yet (e.g. on a fresh install pre-6.1b
    wiring), so missing section is NOT an error.
    """
    if config_path is None or not config_path.is_file():
        return DEFAULT_FORBIDDEN_GLOBS
    try:
        import yaml  # lazy import
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — fall back silently
        return DEFAULT_FORBIDDEN_GLOBS
    if not isinstance(data, dict):
        return DEFAULT_FORBIDDEN_GLOBS
    section = data.get("contribution_forbidden")
    if not isinstance(section, list):
        return DEFAULT_FORBIDDEN_GLOBS
    globs: list[str] = list(DEFAULT_FORBIDDEN_GLOBS)
    for entry in section:
        if isinstance(entry, dict) and "pattern" in entry:
            globs.append(str(entry["pattern"]))
        elif isinstance(entry, str):
            globs.append(entry)
    return tuple(globs)


def _match_any_glob(path: str, globs: tuple[str, ...]) -> str | None:
    """Return the first matching glob, or None."""
    for g in globs:
        if fnmatch(path, g):
            return g
    return None


def _check_forbidden_paths(
    parsed: _ParsedDiff, globs: tuple[str, ...]
) -> list[Finding]:
    hits: list[Finding] = []
    for p in parsed.file_paths:
        matched = _match_any_glob(p, globs)
        if matched:
            hits.append(
                Finding(
                    kind=FindingKind.FORBIDDEN_PATH,
                    severity=Severity.BLOCK,
                    message=f"Diff touches forbidden path: {p}",
                    file=p,
                    scanner="forbidden_paths",
                    detail=f"matches glob {matched!r}",
                )
            )
    return hits


def _check_gitignored_paths(
    parsed: _ParsedDiff, repo_root: Path | None
) -> list[Finding]:
    """Block contributions that touch files excluded by .gitignore.

    Uses ``git check-ignore --no-index`` to check paths against the
    repo's gitignore rules regardless of tracking status.  This catches
    local-only modules and configs that users have gitignored without
    requiring manual forbidden-path maintenance.

    Degrades gracefully: returns [] if *repo_root* is None or git fails.
    """
    if repo_root is None or not parsed.file_paths:
        return []
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "check-ignore", "--no-index", "--stdin"],
            input="\n".join(parsed.file_paths),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return []
    if not proc.stdout.strip():
        return []
    hits: list[Finding] = []
    for ignored_path in proc.stdout.strip().splitlines():
        ignored_path = ignored_path.strip()
        if ignored_path:
            hits.append(
                Finding(
                    kind=FindingKind.GITIGNORED_PATH,
                    severity=Severity.BLOCK,
                    message=f"Diff touches gitignored path: {ignored_path}",
                    file=ignored_path,
                    scanner="gitignored_paths",
                    detail="path matches a .gitignore rule — local-only content",
                )
            )
    return hits


def _check_portability(parsed: _ParsedDiff) -> list[Finding]:
    hits: list[Finding] = []
    for file, line_no, text in parsed.added_lines:
        for regex, label in _PORTABILITY_COMPILED:
            if regex.search(text):
                hits.append(
                    Finding(
                        kind=FindingKind.PORTABILITY,
                        severity=Severity.BLOCK,
                        message=f"Portability hit: {label}",
                        file=file,
                        line=line_no,
                        scanner="portability",
                        detail=text.strip()[:120],
                    )
                )
                # One finding per line is enough; don't multi-flag.
                break
    return hits


def _check_emails(parsed: _ParsedDiff) -> list[Finding]:
    hits: list[Finding] = []
    for file, line_no, text in parsed.added_lines:
        for match in _EMAIL_RE.finditer(text):
            addr = match.group(0)
            if _EMAIL_ALLOWLIST_RE.search(addr):
                continue
            hits.append(
                Finding(
                    kind=FindingKind.EMAIL,
                    severity=Severity.BLOCK,
                    message=f"Personal email address in diff: {addr}",
                    file=file,
                    line=line_no,
                    scanner="email_allowlist",
                    detail=text.strip()[:120],
                )
            )
    return hits


def _check_fingerprints(
    parsed: _ParsedDiff, fingerprint_file: Path | None
) -> list[Finding]:
    if fingerprint_file is None or not fingerprint_file.is_file():
        return []
    try:
        raw = fingerprint_file.read_text(encoding="utf-8")
    except OSError:
        return []
    patterns: list[re.Pattern[str]] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            patterns.append(re.compile(stripped))
        except re.error:
            # Treat as literal if not valid regex
            patterns.append(re.compile(re.escape(stripped)))
    if not patterns:
        return []

    hits: list[Finding] = []
    for file, line_no, text in parsed.added_lines:
        for regex in patterns:
            if regex.search(text):
                hits.append(
                    Finding(
                        kind=FindingKind.FINGERPRINT,
                        severity=Severity.BLOCK,
                        message="Fingerprint match in diff",
                        file=file,
                        line=line_no,
                        scanner="fingerprint",
                        detail=text.strip()[:120],
                    )
                )
                break
    return hits


def _resolve_detect_secrets() -> str | None:
    """Resolve the detect-secrets executable regardless of the process PATH.

    detect-secrets is a declared CORE dependency, installed into the SAME venv as the
    running interpreter. But the sanitizer can run in a process whose PATH lacks that
    venv's bin — e.g. a Claude-Code-spawned MCP child inherits CC's PATH, not the
    server unit's ``PATH=<venv>/bin:...`` — so a bare ``shutil.which`` returns None
    and the floor fail-closed BLOCKS every contribution even though the binary IS
    installed. Prefer the interpreter-relative path (``sys.executable``'s bin dir),
    then fall back to PATH.
    """
    candidate = Path(sys.executable).parent / "detect-secrets"
    if candidate.is_file() and os.access(candidate, os.X_OK):
        return str(candidate)
    return shutil.which("detect-secrets")


def _run_detect_secrets(parsed: _ParsedDiff) -> tuple[bool, list[Finding]]:
    """Run `detect-secrets scan --string` on every added line.

    detect-secrets is the REQUIRED sanitizer floor — if the binary
    isn't available we return (True, [BLOCK finding]) so the overall
    scan fails closed. This matches the plan's framing: no fix ships
    without secret-scanning. bootstrap.sh installs detect-secrets as
    part of the Genesis venv; a missing binary means the install is
    broken and the user must repair it before contributing.

    Resolution is PATH-independent (see ``_resolve_detect_secrets``): the tool can
    run in a process whose PATH lacks the venv bin, and a bare PATH lookup there
    would fail-closed-BLOCK every contribution despite the binary being installed.
    """
    ds_bin = _resolve_detect_secrets()
    if ds_bin is None:
        return True, [
            Finding(
                kind=FindingKind.SECRET,
                severity=Severity.BLOCK,
                message=(
                    "detect-secrets binary not found on PATH. It is the "
                    "required sanitizer floor for community contributions. "
                    "Re-run `pip install -e .` (or activate the Genesis "
                    "venv) to restore it."
                ),
                scanner="detect-secrets",
                detail="missing_binary",
            )
        ]
    if not parsed.added_lines:
        return True, []

    # detect-secrets --string accepts a single string and reports
    # secret-bearing lines. We feed it each added line prefixed with
    # line info so we can map findings back to the source file.
    # --string mode output is key:value pairs, one per plugin that
    # hit. A non-empty response means a finding.
    hits: list[Finding] = []
    for file, line_no, text in parsed.added_lines:
        if not text.strip():
            continue
        try:
            proc = subprocess.run(
                [ds_bin, "scan", "--string", text],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except Exception as exc:  # noqa: BLE001 — REQUIRED fail-closed floor: ANY scan failure BLOCKs
            # Fail CLOSED: the REQUIRED secret-scan floor could not run on this
            # line, so we cannot assert it is secret-free — BLOCK rather than let
            # an unscanned line pass (the old `continue` was a fail-OPEN hole
            # through a fail-CLOSED component). Deliberately broad, NOT an
            # enumerated subtype list — the floor's contract is "any inability to
            # scan == BLOCK", so timeouts, E2BIG/resource errors (OSError, e.g. a
            # >128 KB line over MAX_ARG_STRLEN), a NUL byte in the line (ValueError
            # from execve/text-decode), permission errors, etc. ALL fail closed.
            # Enumerating subtypes one at a time is whack-a-mole (this is the 2nd
            # such miss). KeyboardInterrupt/SystemExit are not Exception subclasses
            # → still propagate; the Finding message carries type(exc).__name__ for
            # operator triage. (STDIN-feeding to remove the 128 KB argv blind spot
            # entirely — so oversized lines get scanned rather than BLOCKed — is a
            # separate hardening follow-up.)
            hits.append(
                Finding(
                    kind=FindingKind.SECRET,
                    severity=Severity.BLOCK,
                    message=f"detect-secrets could not scan a line ({type(exc).__name__}) — failing closed",
                    file=file,
                    line=line_no,
                    scanner="detect-secrets",
                    detail="scan_error",
                )
            )
            continue
        # --string prints lines like "<plugin> : True  (unverified)" /
        # "<plugin> : True  (4.872)" for positives and "<plugin> : False" for
        # negatives. The verdict carries a status/entropy SUFFIX, so match with
        # startswith("true") — an exact `== "true"` silently missed EVERY finding.
        if proc.returncode != 0:
            # Same fail-closed rationale: a nonzero exit means the line was not
            # reliably scanned.
            hits.append(
                Finding(
                    kind=FindingKind.SECRET,
                    severity=Severity.BLOCK,
                    message=f"detect-secrets exited {proc.returncode} on a line — failing closed",
                    file=file,
                    line=line_no,
                    scanner="detect-secrets",
                    detail="nonzero_exit",
                )
            )
            continue
        for out_line in proc.stdout.splitlines():
            if ":" not in out_line:
                continue
            key, _, val = out_line.partition(":")
            if val.strip().lower().startswith("true"):
                hits.append(
                    Finding(
                        kind=FindingKind.SECRET,
                        severity=Severity.BLOCK,
                        message=f"Potential secret ({key.strip()})",
                        file=file,
                        line=line_no,
                        scanner="detect-secrets",
                        detail=text.strip()[:120],
                    )
                )
                break  # one finding per line is sufficient
    return True, hits


def _stderr_tail(proc: subprocess.CompletedProcess) -> str:
    """Last non-empty stderr line, capped + single-line — safe in a finding
    message. gitleaks logs errors (FTL/…) to stderr; it is log text, never the
    scanned secret."""
    lines = [ln for ln in (proc.stderr or "").splitlines() if ln.strip()]
    return lines[-1].strip()[:100] if lines else ""


def _gitleaks_skipped(reason: str) -> Finding:
    """The OPTIONAL gitleaks layer was present but did NOT complete a scan —
    surface a WARN so an operator sees the second layer was skipped rather than a
    silent clean pass. The REQUIRED detect-secrets floor is unaffected and still
    ran; this never blocks on its own."""
    return Finding(
        kind=FindingKind.SECRET,
        severity=Severity.WARN,
        message=(
            f"gitleaks second-layer scan did not complete ({reason}); its "
            "PII/secret rules were NOT applied this run. The required "
            "detect-secrets floor still ran — repair gitleaks or its config "
            "before relying on the second layer."
        ),
        scanner="gitleaks",
        detail=reason,
    )


def _pinned_gitleaks_config() -> str | None:
    """Materialize the COMMITTED `.gitleaks.toml` (`git show HEAD:`) to a temp file
    and return its path, or None to fall back to gitleaks' default rules.

    Reads the config from the trusted server-side install (genesis.env.repo_root())
    at the committed HEAD — NEVER the mutable working tree, and NEVER
    scan_diff()'s caller repo_root — so neither an uncommitted local edit nor an
    untrusted candidate checkout can silently weaken the rules. (`.gitleaks.toml`
    is also on the contribution-forbidden path list, which is the PRIMARY defense:
    a contribution can't merge a change to it; this git-show pin is belt-and-
    suspenders.) The caller unlinks the returned path.
    """
    from genesis.env import repo_root

    try:
        show = subprocess.run(
            ["git", "-C", str(repo_root()), "show", "HEAD:.gitleaks.toml"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None
    if show.returncode != 0 or not show.stdout.strip():
        return None  # not committed / git unavailable -> gitleaks default rules
    fd, path = tempfile.mkstemp(suffix=".gitleaks.toml")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(show.stdout)
    except OSError:
        with contextlib.suppress(OSError):
            os.unlink(path)
        return None
    return path


def _run_gitleaks(
    added_lines: list[tuple[str, int, str]],
) -> tuple[bool, list[Finding]]:
    """Optional second-layer secret scan with gitleaks if installed.

    Scans ONLY the diff's ADDED lines (like _run_detect_secrets and the other
    scanners), so a secret on a REMOVED or context line is never flagged — a
    contribution that DELETES a secret isn't falsely blocked. Runs
    `gitleaks detect --pipe` over the added-line content on stdin, loading the
    genesis PII rules from the COMMITTED `.gitleaks.toml`. Returns
    (scanner_ran, findings).

    gitleaks is the OPTIONAL second layer (detect-secrets is the REQUIRED floor).
    An ABSENT binary → (False, []) — quiet, the layer simply isn't installed. But
    a binary that IS present and ERRORS (bad config, unexpected exit, unparseable
    report) → (True, [WARN]) — VISIBLE, never a silent "ran clean". Empirical
    gitleaks 8.x contract (verified 2026-08):
        exit 0             -> scanned clean (stdout "[]")
        exit 1 + JSON body -> scanned, leaks found
        exit 1 + no report -> ERROR (e.g. a .gitleaks.toml parse failure) — the
                              scan did NOT run. This is the silent-no-scan hole:
                              exit 1 is also the "leaks found" code, so an errored
                              run with empty stdout must WARN, not report clean.
        any other exit     -> ERROR
    Config is pinned to the committed `.gitleaks.toml` (see _pinned_gitleaks_config)
    so a local/uncommitted edit can't weaken the rules; `.gitleaks.toml` is also
    contribution-forbidden so a change to it can't be merged in the first place.

    NOTE: `--no-git` must NOT be combined with `--pipe` — that combination makes
    gitleaks scan nothing from stdin (silent zero findings). NOTE: in `--pipe`
    mode gitleaks never populates a finding's `File`, so `.gitleaks.toml`'s
    `[allowlist] paths` is structurally inert here — a contributor cannot hide a
    secret behind an allowlisted-looking diff path (locked by a regression test).
    """
    gitleaks = shutil.which("gitleaks") or shutil.which("betterleaks")
    if gitleaks is None:
        return False, []  # optional layer absent — not an error, stay quiet
    if not added_lines:
        return True, []

    # Feed ONLY the added-line texts (one per line). Findings are attributed back
    # to the source (file, line) by matching the reported secret text below — NOT
    # by gitleaks' line number, which is unreliable over this header-less --pipe
    # content (0-based, no diff hunk header, version-dependent).
    content = "\n".join(text for _, _, text in added_lines) + "\n"
    # gitleaks --pipe reads stdin since 8.x (NOT with --no-git — see docstring).
    cmd = [gitleaks, "detect", "--pipe", "--report-format", "json",
           "--report-path", "/dev/stdout"]

    tmp_config: str | None = None
    try:
        tmp_config = _pinned_gitleaks_config()
        if tmp_config is not None:
            cmd += ["-c", tmp_config]

        try:
            proc = subprocess.run(
                cmd,
                input=content,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
            return True, [_gitleaks_skipped(type(exc).__name__)]

        rc = proc.returncode
        stdout = proc.stdout.strip()
        # Only exit 0 (clean) and exit 1 WITH a JSON body (leaks) are successful
        # scans. Everything else means gitleaks did not actually scan — surface a
        # WARN, never report clean (the exit-1/empty-stdout case is the hole).
        if rc not in (0, 1):
            return True, [_gitleaks_skipped(f"exit {rc}: {_stderr_tail(proc)}")]
        if not stdout:
            if rc == 1:
                return True, [_gitleaks_skipped(f"exit 1 with no report: {_stderr_tail(proc)}")]
            return True, []  # exit 0, no body -> nothing found
        try:
            findings = json.loads(stdout)
        except json.JSONDecodeError:
            return True, [_gitleaks_skipped("report was not valid JSON")]

        hits: list[Finding] = []
        if isinstance(findings, list):
            for f in findings:
                if not isinstance(f, dict):
                    continue
                rule = f.get("RuleID", "unknown")
                # Attribute by matching the reported secret text back to the added
                # line that contains it — robust to gitleaks' unreliable --pipe
                # line numbers. If it can't be located, leave (file, line) unset;
                # the finding is BLOCK either way.
                match_text = f.get("Secret") or f.get("Match", "")
                src_file, src_line = None, None
                if match_text:
                    for fpath, lno, text in added_lines:
                        if match_text in text:
                            src_file, src_line = fpath, lno
                            break
                hits.append(
                    Finding(
                        kind=FindingKind.SECRET,
                        severity=Severity.BLOCK,
                        message=f"Potential secret ({rule})",
                        file=src_file,
                        line=src_line,
                        scanner="gitleaks",
                        detail=f.get("Match", "")[:120],
                    )
                )
        return True, hits
    finally:
        if tmp_config is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp_config)


def scan_diff(
    diff_text: str,
    *,
    protected_paths_yaml: Path | None = None,
    fingerprint_file: Path | None = None,
    repo_root: Path | None = None,
) -> SanitizerResult:
    """Main entry point. Scan a unified diff and return a SanitizerResult.

    Args:
        diff_text: Full unified diff as produced by `git show <sha>`
            or `git format-patch --stdout`.
        protected_paths_yaml: Path to config/protected_paths.yaml.
            Defaults to DEFAULT_FORBIDDEN_GLOBS if None.
        fingerprint_file: Path to user fingerprint list. Defaults to
            $GENESIS_RELEASE_FINGERPRINTS or
            ~/.genesis/release-fingerprints.txt.
        repo_root: Path to the git repo root. When provided, enables
            the gitignored-paths scanner which blocks contributions
            touching files excluded by .gitignore.
    """
    if fingerprint_file is None:
        env_path = os.environ.get("GENESIS_RELEASE_FINGERPRINTS")
        if env_path:
            fingerprint_file = Path(env_path)
        else:
            fingerprint_file = Path.home() / ".genesis" / "release-fingerprints.txt"

    parsed = parse_diff(diff_text)
    findings: list[Finding] = []
    scanners_run: list[str] = []

    # 1. Size cap (before any expensive work)
    if parsed.size_bytes > MAX_DIFF_BYTES:
        findings.append(
            Finding(
                kind=FindingKind.SIZE,
                severity=Severity.BLOCK,
                message=(
                    f"Diff is {parsed.size_bytes} bytes, exceeds "
                    f"MVP cap of {MAX_DIFF_BYTES} bytes. Large diffs "
                    "are out of scope for community contributions in Phase 6.1."
                ),
                scanner="size_cap",
            )
        )
    scanners_run.append("size_cap")

    # 2. Binary files
    if parsed.is_binary:
        findings.append(
            Finding(
                kind=FindingKind.BINARY,
                severity=Severity.BLOCK,
                message="Diff contains binary file changes. Binary "
                        "contributions are not supported in MVP.",
                scanner="binary_check",
            )
        )
    scanners_run.append("binary_check")

    # 3. Forbidden paths
    globs = _load_forbidden_globs(protected_paths_yaml)
    findings.extend(_check_forbidden_paths(parsed, globs))
    scanners_run.append("forbidden_paths")

    # 3b. Gitignored paths (only when repo_root provided)
    if repo_root is not None:
        findings.extend(_check_gitignored_paths(parsed, repo_root))
        scanners_run.append("gitignored_paths")

    # 4. Portability
    findings.extend(_check_portability(parsed))
    scanners_run.append("portability")

    # 5. Email allowlist
    findings.extend(_check_emails(parsed))
    scanners_run.append("email_allowlist")

    # 6. Fingerprints (optional — only runs if file exists). The generic
    #    portability classes above are always-on, but this install's EXACT
    #    non-classable values (private repo/host names, hardware, tokens) are
    #    only caught when the fingerprint file is present. A missing file is a
    #    provisioning gap, so surface it as a non-blocking WARN (does not affect
    #    `ok`) rather than silently skipping — run bootstrap to generate it.
    #    (The awareness posture check is the standing-alert companion to this.)
    if fingerprint_file and fingerprint_file.is_file():
        findings.extend(_check_fingerprints(parsed, fingerprint_file))
        scanners_run.append("fingerprint")
    else:
        # File absent: the exact-value scan did NOT run, so do NOT record it in
        # scanners_run (whose contract is scanners that actually executed — the
        # CLI/PR body render that list). The WARN finding is the explicit signal
        # that exact-value coverage was skipped.
        findings.append(
            Finding(
                kind=FindingKind.FINGERPRINT,
                severity=Severity.WARN,
                message=(
                    "Release fingerprint file not found — this install's exact "
                    "private identifiers are not being scanned. Run bootstrap "
                    "(or `python -m genesis.contribution.fingerprints --write`) "
                    "to generate it."
                ),
                scanner="fingerprint",
                detail=str(fingerprint_file),
            )
        )

    # 7. detect-secrets (required floor)
    ran, secret_hits = _run_detect_secrets(parsed)
    if ran:
        scanners_run.append("detect-secrets")
        findings.extend(secret_hits)

    # 8. gitleaks (optional second layer)
    ran, gl_hits = _run_gitleaks(parsed.added_lines)
    if ran:
        scanners_run.append("gitleaks")
        findings.extend(gl_hits)

    ok = not any(f.severity == Severity.BLOCK for f in findings)
    return SanitizerResult(ok=ok, findings=findings, scanners_run=scanners_run)


def _parse_prose(text: str) -> _ParsedDiff:
    """Wrap plain prose as a ``_ParsedDiff`` whose ``added_lines`` are EVERY
    line of the text.

    ``parse_diff`` only keeps ``+``-prefixed lines under a ``+++ b/<path>``
    header, so it sees NOTHING in plain prose (an issue body, a comment) and a
    scan of it fails OPEN. This wrapper hands every line to the raw-string
    detectors instead. ``file`` is a synthetic ``"<prose>"`` label; line
    numbers are 1-based to match a human reading the text.
    """
    added: list[tuple[str, int, str]] = [
        ("<prose>", i, line) for i, line in enumerate(text.splitlines(), start=1)
    ]
    return _ParsedDiff(
        file_paths=[],
        added_lines=added,
        is_binary=False,
        size_bytes=len(text.encode("utf-8")),
    )


def scan_prose(
    text: str,
    *,
    fingerprint_file: Path | None = None,
) -> SanitizerResult:
    """Fail-closed sanitizer for free TEXT (a drafted issue body, a comment) —
    the prose analog of :func:`scan_diff`.

    Runs ONLY the raw-string detectors that are meaningful on prose, over
    EVERY line of *text*: portability classes (IPs, ``/home/<user>`` paths, CC
    slugs), personal emails outside the allowlist, user fingerprints, and the
    required ``detect-secrets`` floor. The diff-STRUCTURAL scanners
    (forbidden-path, gitignore, binary, size) are deliberately skipped — they
    describe a unified diff's shape, not prose content.

    Do NOT reach for :func:`scan_diff` on prose: it routes through
    :func:`parse_diff`, which drops every non-``+`` line, so plain text yields
    zero ``added_lines`` and the scan returns ``ok=True`` regardless of what it
    contains — a fail-OPEN hole through a fail-CLOSED component. This function
    closes it.

    Same contract as :func:`scan_diff`: ``ok`` is True iff no BLOCK finding.
    Fail-closed on BOTH required guards: a missing ``detect-secrets`` binary
    BLOCKs, and — because prose is the TERMINAL egress guard with no CI backstop
    (unlike scan_diff, whose PR output CI re-scans) — a missing fingerprint file
    also BLOCKs rather than surfacing a soft WARN.
    """
    if fingerprint_file is None:
        env_path = os.environ.get("GENESIS_RELEASE_FINGERPRINTS")
        if env_path:
            fingerprint_file = Path(env_path)
        else:
            fingerprint_file = Path.home() / ".genesis" / "release-fingerprints.txt"

    parsed = _parse_prose(text)
    findings: list[Finding] = []
    scanners_run: list[str] = []

    # Portability (IPs, /home/<user> paths, CC slugs)
    findings.extend(_check_portability(parsed))
    scanners_run.append("portability")

    # Personal emails outside the allowlist
    findings.extend(_check_emails(parsed))
    scanners_run.append("email_allowlist")

    # Fingerprints. Unlike scan_diff (whose output is a PR that CI re-scans
    # against the private-pattern superset), scan_prose is the TERMINAL guard for
    # a runtime `gh issue create` egress — no downstream backstop. So a missing
    # fingerprint file fails CLOSED (BLOCK), not a soft WARN.
    if fingerprint_file and fingerprint_file.is_file():
        findings.extend(_check_fingerprints(parsed, fingerprint_file))
        scanners_run.append("fingerprint")
    else:
        findings.append(
            Finding(
                kind=FindingKind.FINGERPRINT,
                severity=Severity.BLOCK,
                message=(
                    "Release fingerprint file not found — this install's exact "
                    "private identifiers cannot be scanned, and prose egresses "
                    "with no CI backstop. Failing closed. Run bootstrap (or "
                    "`python -m genesis.contribution.fingerprints --write`)."
                ),
                scanner="fingerprint",
                detail=str(fingerprint_file),
            )
        )

    # detect-secrets (required floor — a missing binary BLOCKs, fail-closed)
    ran, secret_hits = _run_detect_secrets(parsed)
    if ran:
        scanners_run.append("detect-secrets")
        findings.extend(secret_hits)

    ok = not any(f.severity == Severity.BLOCK for f in findings)
    return SanitizerResult(ok=ok, findings=findings, scanners_run=scanners_run)
