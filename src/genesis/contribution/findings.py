"""Phase 6 contribution — shared dataclasses and enums.

All other `genesis.contribution` modules import from here. Keep this
module dependency-free (stdlib only) so it can be imported from
lightweight hook contexts.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Severity(StrEnum):
    """Finding severity. BLOCK findings always fail the sanitizer."""

    INFO = "info"
    WARN = "warn"
    BLOCK = "block"


class FindingKind(StrEnum):
    """Taxonomy of things the sanitizer can detect."""

    SECRET = "secret"                  # detect-secrets / gitleaks hit  # pragma: allowlist secret
    PORTABILITY = "portability"        # IPs, hostnames, absolute paths
    FINGERPRINT = "fingerprint"        # user-defined fingerprints
    EMAIL = "email"                    # personal email (outside allowlist)
    FORBIDDEN_PATH = "forbidden_path"  # diff touches CONTRIBUTION_FORBIDDEN
    BINARY = "binary"                  # binary file in diff
    SIZE = "size"                      # diff too large


@dataclass
class Finding:
    """One sanitizer finding. All user-visible details live here."""

    kind: FindingKind
    severity: Severity
    message: str
    file: str | None = None   # path the finding applies to, if known
    line: int | None = None   # 1-based line in the diff, if known
    scanner: str | None = None  # "detect-secrets", "gitleaks", "portability", etc.
    detail: str | None = None   # short snippet / reason

    def to_dict(self) -> dict:
        return {
            "kind": self.kind.value,
            "severity": self.severity.value,
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "scanner": self.scanner,
            "detail": self.detail,
        }


@dataclass
class SanitizerResult:
    """Return value of `scan_diff()`.

    `ok` is True iff no BLOCK-severity findings exist. WARN/INFO
    findings do not fail the sanitizer but are attached for display.
    `scanners_run` lists the scanners that actually executed — useful
    for the PR body ("Sanitizer: detect-secrets, portability, 0 findings").
    """

    ok: bool
    findings: list[Finding] = field(default_factory=list)
    scanners_run: list[str] = field(default_factory=list)

    def blocking(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == Severity.BLOCK]

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "findings": [f.to_dict() for f in self.findings],
            "scanners_run": list(self.scanners_run),
        }


@dataclass
class ReviewResult:
    """Result of the adversarial review fallback chain.

    `available=False` means the full chain failed and the PR will ship
    with "Review: unavailable". `findings` is free-form text from the
    winning reviewer — not a list of Finding objects, since each
    reviewer produces different output shapes.
    """

    available: bool
    reviewer: str | None = None        # "codex" | "cc-reviewer" | "genesis-native"
    passed: bool = False               # reviewer's own verdict
    finding_count: int = 0             # reviewer's own count
    summary: str = ""                  # one-line human summary
    raw: str = ""                      # full reviewer output for the PR body

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "reviewer": self.reviewer,
            "passed": self.passed,
            "finding_count": self.finding_count,
            "summary": self.summary,
            "raw": self.raw,
        }


@dataclass
class VersionGateResult:
    """Result of the semantic "already fixed upstream?" check."""

    # True = contribution cancelled because the fix is already upstream
    already_fixed: bool
    confidence: int                    # 0-100, from the LLM
    matched_sha: str | None = None     # upstream commit that looked like a match
    reasoning: str = ""                # one-sentence LLM rationale
    version_match: bool = False        # True if install SHA == upstream HEAD
    upstream_commit_count: int = 0     # how many commits the user is behind
    parse_ok: bool = True              # False if LLM response was unparseable
    llm_error: str | None = None       # error message if LLM call failed

    def to_dict(self) -> dict:
        return {
            "already_fixed": self.already_fixed,
            "confidence": self.confidence,
            "matched_sha": self.matched_sha,
            "reasoning": self.reasoning,
            "version_match": self.version_match,
            "upstream_commit_count": self.upstream_commit_count,
            "parse_ok": self.parse_ok,
            "llm_error": self.llm_error,
        }


@dataclass
class DivergenceResult:
    """Result of the `git merge-tree` divergence check."""

    clean: bool                        # True = no conflict, safe to PR
    conflict_files: list[str] = field(default_factory=list)
    message: str = ""                  # human-readable explanation

    def to_dict(self) -> dict:
        return {
            "clean": self.clean,
            "conflict_files": list(self.conflict_files),
            "message": self.message,
        }


@dataclass
class InstallInfo:
    """Local install identity. Persisted at ~/.genesis/install.json."""

    install_id: str                    # UUID4 string
    created_at: str                    # ISO8601 UTC
    fingerprint_file: str | None = None  # optional path to user fingerprints

    def to_dict(self) -> dict:
        return {
            "install_id": self.install_id,
            "created_at": self.created_at,
            "fingerprint_file": self.fingerprint_file,
        }
