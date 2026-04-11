"""Phase 6 community contribution pipeline — public API.

See docs/architecture and the Phase 6 plan for design details.
Core library (6.1b.1) contains no side effects and no external
integration — that's 6.1b.2.
"""
from .divergence import check_divergence
from .findings import (
    DivergenceResult,
    Finding,
    FindingKind,
    InstallInfo,
    ReviewResult,
    SanitizerResult,
    Severity,
    VersionGateResult,
)
from .identity import get_install_id, load_install_info, pseudonym_email
from .pr_opener import PRCreationResult, build_pr_body, create_pr
from .review import run_review_chain, write_review_log
from .sanitize import (
    DEFAULT_FORBIDDEN_GLOBS,
    MAX_DIFF_BYTES,
    parse_diff,
    scan_diff,
)
from .version_gate import (
    CONFIDENCE_THRESHOLD,
    VERSION_GATE_PROMPT,
    build_prompt,
    check_version_gate,
    fetch_upstream_log,
    format_version_string,
    parse_llm_response,
    read_install_sha,
    read_install_version,
)

__all__ = [
    "CONFIDENCE_THRESHOLD",
    "DEFAULT_FORBIDDEN_GLOBS",
    "DivergenceResult",
    "Finding",
    "FindingKind",
    "InstallInfo",
    "MAX_DIFF_BYTES",
    "PRCreationResult",
    "ReviewResult",
    "SanitizerResult",
    "Severity",
    "VERSION_GATE_PROMPT",
    "VersionGateResult",
    "build_pr_body",
    "build_prompt",
    "check_divergence",
    "check_version_gate",
    "create_pr",
    "fetch_upstream_log",
    "format_version_string",
    "get_install_id",
    "load_install_info",
    "parse_diff",
    "parse_llm_response",
    "pseudonym_email",
    "read_install_sha",
    "read_install_version",
    "run_review_chain",
    "scan_diff",
    "write_review_log",
]
