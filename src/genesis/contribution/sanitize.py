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
6. Portability patterns — IPs, /home/ubuntu/ paths, hardcoded
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

import json
import os
import re
import shutil
import subprocess
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
)

# Portability patterns — things that should never appear in a
# public-facing contribution. Copied from the release script's
# phase 8 portability scan.
_PORTABILITY_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"/home/ubuntu/genesis", "absolute path /home/ubuntu/genesis"),
    (r"/home/ubuntu/agent-zero", "absolute path /home/ubuntu/agent-zero"),
    (r"/home/ubuntu/\.[A-Za-z]", "absolute path to user dotfile"),
    (r"-home-ubuntu-genesis", "CC project dir slug"),
    (r"10\.176\.34\.199", "Ollama host IP"),
    (r"10\.176\.34\.206", "container IP"),
    (r"192\.168\.50\.\d+", "private subnet IP"),
    (r"\bWingedGuardian/(Genesis|genesis-backups)\b", "private repo reference"),
    (r"\bAmerica/New_York\b", "hardcoded user timezone"),
    (r"\bfd42:e3ba\b", "container IPv6 prefix"),
    (r"\bfd4d:77b8\b", "host IPv6 prefix"),
    (r"\b5070ti\b", "hardware reference"),
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


def _run_detect_secrets(parsed: _ParsedDiff) -> tuple[bool, list[Finding]]:
    """Run `detect-secrets scan --string` on every added line.

    detect-secrets is the REQUIRED sanitizer floor — if the binary
    isn't available we return (True, [BLOCK finding]) so the overall
    scan fails closed. This matches the plan's framing: no fix ships
    without secret-scanning. bootstrap.sh installs detect-secrets as
    part of the Genesis venv; a missing binary means the install is
    broken and the user must repair it before contributing.
    """
    if shutil.which("detect-secrets") is None:
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
                ["detect-secrets", "scan", "--string", text],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            continue
        # --string prints lines like "<plugin>: True" for positives and
        # "<plugin>: False" for negatives. Parse for any True.
        if proc.returncode != 0:
            continue
        for out_line in proc.stdout.splitlines():
            if ":" not in out_line:
                continue
            key, _, val = out_line.partition(":")
            if val.strip().lower() == "true":
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


def _run_gitleaks(diff_text: str) -> tuple[bool, list[Finding]]:
    """Optional second-layer scan with gitleaks if installed.

    Runs `gitleaks detect --no-git --pipe` with the diff on stdin
    (via a temp path, since gitleaks needs a file). Returns
    (scanner_ran, findings).
    """
    gitleaks = shutil.which("gitleaks") or shutil.which("betterleaks")
    if gitleaks is None:
        return False, []

    # gitleaks --pipe reads stdin since 8.x. Use that when available.
    try:
        proc = subprocess.run(
            [gitleaks, "detect", "--no-git", "--pipe", "--report-format", "json",
             "--report-path", "/dev/stdout"],
            input=diff_text,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False, []

    hits: list[Finding] = []
    # gitleaks exits 1 when findings exist. Parse JSON from stdout.
    stdout = proc.stdout.strip()
    if not stdout:
        return True, []
    try:
        findings = json.loads(stdout)
    except json.JSONDecodeError:
        return True, []
    if isinstance(findings, list):
        for f in findings:
            if not isinstance(f, dict):
                continue
            rule = f.get("RuleID", "unknown")
            file = f.get("File", "")
            line = f.get("StartLine", 0)
            hits.append(
                Finding(
                    kind=FindingKind.SECRET,
                    severity=Severity.BLOCK,
                    message=f"Potential secret ({rule})",
                    file=file or None,
                    line=int(line) if line else None,
                    scanner="gitleaks",
                    detail=f.get("Match", "")[:120],
                )
            )
    return True, hits


def scan_diff(
    diff_text: str,
    *,
    protected_paths_yaml: Path | None = None,
    fingerprint_file: Path | None = None,
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

    # 4. Portability
    findings.extend(_check_portability(parsed))
    scanners_run.append("portability")

    # 5. Email allowlist
    findings.extend(_check_emails(parsed))
    scanners_run.append("email_allowlist")

    # 6. Fingerprints (optional — only runs if file exists)
    if fingerprint_file and fingerprint_file.is_file():
        findings.extend(_check_fingerprints(parsed, fingerprint_file))
        scanners_run.append("fingerprint")

    # 7. detect-secrets (required floor)
    ran, secret_hits = _run_detect_secrets(parsed)
    if ran:
        scanners_run.append("detect-secrets")
        findings.extend(secret_hits)

    # 8. gitleaks (optional second layer)
    ran, gl_hits = _run_gitleaks(diff_text)
    if ran:
        scanners_run.append("gitleaks")
        findings.extend(gl_hits)

    ok = not any(f.severity == Severity.BLOCK for f in findings)
    return SanitizerResult(ok=ok, findings=findings, scanners_run=scanners_run)
