#!/usr/bin/env python3
"""Census LLM-provider routing-bypass references and external egress outside routing.

This is a review tripwire, NOT a spend barrier. Runtime credential isolation
for tests is enforced by ``tests/conftest.py`` (``_isolate_credentials``).
Non-LLM credentialed hosts (TTS, Brave, Tinyfish, Firecrawl) are out of census
scope.

References are count-baselined per file and per class. A new reference exceeds
its pin; a removed reference makes the pin stale. Swapping one reference for
another at the same count passes. Production scans ``src/genesis``, ``scripts``,
and ``tests`` across executable Python, shell, JavaScript, TypeScript, and
notebook files.

WHAT IT DOES NOT SEE (a reference tripwire, not taint analysis):
  * A pre-built URL variable: ``channels/discord_adapter.py`` receives its
    webhook from ``runtime/init/outreach.py``; the guard covers its
    ``DISCORD_WEBHOOK_URL`` origin.
  * SDK clients with implicit hosts (for example, TinyFish and Firecrawl).
OUT OF SCOPE:
  * Browser-based publishing (for example, Medium via Playwright).
  * Owner-private channels (email or Telegram to the owner).

Usage:  python scripts/check_external_io.py   (exit 0 = clean, 1 = violation)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parents[1]
SCAN_ROOTS = ("src/genesis", "scripts", "tests")
SCAN_SUFFIXES = (".py", ".sh", ".bash", ".js", ".mjs", ".ts", ".ipynb")


class Violation(NamedTuple):
    path: str
    lineno: int
    line: str
    cls: str
    kind: str


def _host(host_re: str) -> str:
    """Reject extra DNS labels and dot continuations; allow subdomains, ports, and paths."""
    return rf"(?<![a-zA-Z0-9-]){host_re}(?![a-zA-Z0-9.-])"


PATTERNS: list[re.Pattern[str]] = [
    re.compile(_host(r"discord\.com") + r"(?::\d+)?/api", re.IGNORECASE),
    re.compile(_host(r"discordapp\.com") + r"(?::\d+)?/api", re.IGNORECASE),
    re.compile(r"DISCORD_WEBHOOK"),
    re.compile(_host(r"api\.twitter\.com"), re.IGNORECASE),
    re.compile(_host(r"slack\.com") + r"(?::\d+)?/api", re.IGNORECASE),
    re.compile(_host(r"hooks\.slack\.com"), re.IGNORECASE),
]

PROVIDER_HOSTS: tuple[str, ...] = (
    "api.openai.com",
    "api.anthropic.com",
    "api.x.ai",
    "integrate.api.nvidia.com",
    "openrouter.ai",
    "api.mistral.ai",
    "api.groq.com",
    "api.deepinfra.com",
    "zenmux.ai",
    "api.minimaxi.com",
    "api.minimax.com",
    "dashscope.aliyuncs.com",
    "generativelanguage.googleapis.com",
    "api.deepseek.com",
    "api.perplexity.ai",
    "api.voyageai.com",
)
PROVIDER_PATTERNS = [re.compile(_host(re.escape(host)), re.IGNORECASE) for host in PROVIDER_HOSTS]

# Hosts used at call sites but not yet in either shipped provider inventory.
CALL_SITE_ONLY_HOSTS: dict[str, str] = {
    "api.deepinfra.com": "memory/embeddings.py call site",
    "dashscope.aliyuncs.com": "memory/embeddings.py call site",
    "api.perplexity.ai": "research/perplexity.py call site",
    "api.voyageai.com": "memory/reranker.py call site",
    "api.minimax.com": "alternate MiniMax spelling covered by the rule YAML",
}

BASELINE: dict[tuple[str, str], tuple[int, str]] = {
    ("scripts/behavioral_linter.py", "provider"): (1, "linter inventory/docs"),
    ("scripts/check_external_io.py", "egress"): (2, "guard egress patterns"),
    ("scripts/check_external_io.py", "provider"): (22, "guard provider inventory"),
    ("scripts/install.sh", "provider"): (4, "installer probe, key validation, help text"),
    ("src/genesis/cc/invoker.py", "provider"): (1, "comment: api.anthropic.com outage note"),
    ("src/genesis/channels/stt.py", "provider"): (1, "Groq STT call site"),
    ("src/genesis/deliberation/backends/fusion.py", "provider"): (1, "OpenRouter fusion call site"),
    ("src/genesis/eval/longmemeval/client.py", "provider"): (1, "OpenRouter eval client"),
    ("src/genesis/mcp/discord_bot_mcp.py", "egress"): (1, "Discord API send; shadow-observed"),
    ("src/genesis/mcp/outreach_mcp.py", "egress"): (4, "Discord webhook polling"),
    ("src/genesis/memory/embeddings.py", "provider"): (2, "embedding provider calls"),
    ("src/genesis/memory/reranker.py", "provider"): (1, "Voyage rerank call site"),
    ("src/genesis/observability/provider_health.py", "provider"): (9, "health probes"),
    ("src/genesis/observability/snapshots/api_keys.py", "provider"): (7, "key probes"),
    ("src/genesis/recon/model_intelligence.py", "provider"): (1, "OpenRouter model metadata fetch"),
    ("src/genesis/research/perplexity.py", "provider"): (1, "Perplexity research call site"),
    ("src/genesis/routing/litellm_delegate.py", "provider"): (1, "sanctioned routing call path"),
    ("src/genesis/runtime/init/outreach.py", "egress"): (10, "gated webhook wiring"),
    ("tests/conftest.py", "egress"): (1, "test-only webhook credential pin"),
    ("tests/test_cc/test_roster.py", "provider"): (1, "roster routing fixture"),
    ("tests/test_channels/test_discord_adapter.py", "egress"): (12, "adapter fixture literals"),
    ("tests/test_credential_isolation.py", "egress"): (1, "webhook credential-pin test"),
    ("tests/test_deliberation/test_deliberate.py", "provider"): (1, "deliberation fixtures"),
    ("tests/test_hooks/test_behavioral_linter.py", "provider"): (37, "provider linter fixtures"),
    ("tests/test_hooks/test_inline_hooks.py", "provider"): (1, "inline-hook fixtures"),
    ("tests/test_hooks/test_secret_scrub.py", "egress"): (6, "scrub fixtures"),
    ("tests/test_learning/test_procedure_extraction.py", "provider"): (1, "extraction fixture"),
    ("tests/test_mcp/test_capability_shadow_wiring.py", "egress"): (2, "shadow wiring fixture"),
    ("tests/test_mcp/test_outreach_mcp.py", "egress"): (22, "webhook environment fixtures"),
    ("tests/test_memory/test_reranker.py", "provider"): (1, "reranker fixture"),
    ("tests/test_observability/test_key_validator.py", "provider"): (3, "key-validator fixtures"),
    ("tests/test_outreach/test_pipeline.py", "egress"): (1, "pipeline fixture"),
    ("tests/test_routing/test_provider_health_probes.py", "provider"): (5, "health-probe fixtures"),
    ("tests/test_scripts/test_check_external_io.py", "egress"): (10, "guard egress fixtures"),
    ("tests/test_scripts/test_check_external_io.py", "provider"): (17, "guard provider fixtures"),
}
CLASSES: dict[str, list[re.Pattern[str]]] = {
    "egress": PATTERNS,
    "provider": PROVIDER_PATTERNS,
}


def _iter_files(root: Path, subdirs: tuple[str, ...] | list[str] | None):
    scan_roots = (root,) if subdirs is None else tuple(root / subdir for subdir in subdirs)
    paths = set()
    for scan_root in scan_roots:
        if scan_root.is_dir():
            paths.update(scan_root.rglob("*"))
    for path in sorted(paths):
        if path.is_file() and path.suffix in SCAN_SUFFIXES and "node_modules" not in path.parts:
            yield path


def _within_subdirs(relpath: str, subdirs: tuple[str, ...] | list[str] | None) -> bool:
    if subdirs is None:
        return True
    path = Path(relpath)
    return any(path.is_relative_to(Path(subdir)) for subdir in subdirs)


def _merged_spans(line: str, patterns: list[re.Pattern[str]]) -> list[tuple[int, int]]:
    spans = sorted(
        (match.start(), match.end()) for pattern in patterns for match in pattern.finditer(line)
    )
    merged: list[tuple[int, int]] = []
    for start, end in spans:
        if merged and start < merged[-1][1]:
            previous_start, previous_end = merged[-1]
            merged[-1] = (previous_start, max(previous_end, end))
        else:
            merged.append((start, end))
    return merged


def scan(
    root: Path,
    *,
    baseline: dict[tuple[str, str], tuple[int, str]] | None = None,
    classes: tuple[str, ...] | list[str] | set[str] | None = None,
    subdirs: tuple[str, ...] | list[str] | None = None,
) -> list[Violation]:
    """Return census violations for the selected pattern classes under root."""
    root = Path(root).resolve()
    pins = BASELINE if baseline is None else baseline
    selected = tuple(CLASSES) if classes is None else tuple(classes)
    unknown = set(selected) - CLASSES.keys()
    if unknown:
        raise ValueError(f"unknown external-I/O classes: {sorted(unknown)}")

    matches: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for path in _iter_files(root, subdirs):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        relpath = path.relative_to(root).as_posix()
        for cls in selected:
            patterns = CLASSES[cls]
            for lineno, line in enumerate(lines, start=1):
                for _start, _end in _merged_spans(line, patterns):
                    matches.setdefault((relpath, cls), []).append((lineno, line.strip()))

    violations: list[Violation] = []
    for key, file_matches in matches.items():
        expected = pins.get(key)
        relpath, cls = key
        if expected is None:
            violations.extend(
                Violation(relpath, lineno, line, cls, "unlisted") for lineno, line in file_matches
            )
            continue
        expected_count, _reason = expected
        actual_count = len(file_matches)
        if actual_count > expected_count:
            violations.extend(
                Violation(relpath, lineno, line, cls, "over_baseline")
                for lineno, line in file_matches
            )

    for (relpath, cls), (expected_count, reason) in pins.items():
        if cls not in selected or not _within_subdirs(relpath, subdirs):
            continue
        baseline_path = (root / relpath).resolve()
        try:
            baseline_path.relative_to(root)
        except ValueError:
            continue
        actual_count = len(matches.get((relpath, cls), ()))
        if not baseline_path.is_file() or actual_count == 0 or actual_count < expected_count:
            violations.append(
                Violation(
                    relpath,
                    0,
                    f"expected {expected_count} refs, found {actual_count}; {reason}",
                    cls,
                    "stale_baseline",
                )
            )

    return violations


def _guidance(cls: str) -> str:
    if cls == "egress":
        return "External-world egress: route the send through the capability gate."
    return (
        "Provider endpoint: route the call through the Genesis routing layer "
        "so it is budgeted and tracked, or update its per-class baseline."
    )


def main() -> int:
    for subdir in SCAN_ROOTS:
        if not (REPO_ROOT / subdir).is_dir():
            print(f"scan root {subdir} not found")
            return 1
    violations = scan(REPO_ROOT, subdirs=SCAN_ROOTS)
    if not violations:
        print("External-I/O census: CLEAN")
        return 0

    print("::error::External-I/O census violations detected.")
    for cls in CLASSES:
        class_violations = [item for item in violations if item.cls == cls]
        if not class_violations:
            continue
        print(_guidance(cls))
        for violation in class_violations:
            detail = f" [{violation.kind}]"
            if violation.kind == "over_baseline":
                count = sum(
                    item.path == violation.path and item.cls == cls and item.kind == "over_baseline"
                    for item in class_violations
                )
                expected = BASELINE[(violation.path, cls)][0]
                detail += f" ({count} refs, baseline {expected})"
            print(f"  {violation.path}:{violation.lineno}: {violation.line}{detail}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
