#!/usr/bin/env python3
"""External-I/O regression guard — no NEW ungated egress or provider references.

Two check classes share one mechanism: a file that references a covered
endpoint must be on the matching allowlist (each entry pinned to a known,
reasoned holder). A new door added anywhere else fails the check — forcing it
through the sanctioned layer instead of silently shipping a bypass.

CLASS 1 — external-world egress (the original WS5 contract): Genesis must not
post to the outside world (Discord community, public webhooks, public social
APIs) through an ungated, unobserved path. Any source file that REFERENCES a
Discord/webhook/public-social endpoint or webhook env var must be allowlisted.

CLASS 2 — provider-endpoint references (added for #2230's test-file gap): the
behavioral linter's no-raw-provider-calls rule exempts `tests/` on the premise
that a CI backstop sees them — no backstop existed until this class was added.
Any file under a scan root that REFERENCES a provider host (chat, embeddings,
rerank, audio, images — spend or key-validation surface alike) must be
allowlisted. The allowlist IS the sanctioned-layer inventory: routing,
provider call sites, health/key probes, and test fixtures.

WHAT IT DOES NOT SEE (by design — a reference tripwire, not a taint analysis):
  * A ``.post(pre_built_url)`` whose URL arrives as a variable with no literal
    endpoint token on the call line — e.g. ``channels/discord_adapter.py``
    posts to a webhook URL it RECEIVES from ``runtime/init/outreach.py``. The
    guard covers that URL's ORIGIN (the DISCORD_WEBHOOK reference in
    outreach.py, allow-listed) — which is exactly what a new door must add —
    rather than the generic POST site itself.
  * SDK-driven clients with no literal host (TinyFish, Firecrawl read their
    endpoint from the SDK/env, not a literal) — nothing to grep. A new
    consumer must still reference the cred env var, which is a different
    tripwire's job.
  * Shell scripts and prose: the scan covers ``*.py`` only.
OUT OF SCOPE (handled elsewhere):
  * Browser-based publishing (e.g. Medium via Playwright) — a different egress
    modality; a browser-egress guard lands with that channel's gating stage.
  * Owner-private notification channels (email-to-owner, Telegram-to-owner) —
    the owner IS the recipient; these are not external-world posting.

Usage:  python scripts/check_external_io.py   (exit 0 = clean, 1 = violation)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SCAN_ROOT = Path("src/genesis")  # kept for the single-root callers/tests
SCAN_ROOTS = (Path("src/genesis"), Path("scripts"), Path("tests"))

# Endpoint/webhook-env signatures for autonomous external-world HTTP egress.
PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"discord\.com/api"),        # Discord REST API (send_reply)
    re.compile(r"discordapp\.com/api"),     # legacy Discord API
    re.compile(r"DISCORD_WEBHOOK"),         # Discord webhook env (adapter / outreach_poll)
    re.compile(r"api\.twitter\.com"),       # Twitter/X (future public channel)
    re.compile(r"slack\.com/api"),          # Slack API (future)
    re.compile(r"hooks\.slack\.com"),       # Slack webhooks (future)
]

# LLM/ML provider hosts — union of the routing inventory (`provider_health.
# _PROVIDER_URLS` + model_routing.yaml `base_url`s) and the call sites that
# exist in-tree (Perplexity research, Voyage rerank). Host-level, not
# path-level: a reference to the host is the door the spend walks through,
# and this check's job is that the file is KNOWN — path-level calls vs reads
# is the behavioral linter's question.
PROVIDER_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"api\.openai\.com"),
    re.compile(r"api\.anthropic\.com"),
    re.compile(r"api\.x\.ai"),
    re.compile(r"integrate\.api\.nvidia\.com"),
    re.compile(r"openrouter\.ai"),
    re.compile(r"api\.mistral\.ai"),
    re.compile(r"api\.groq\.com"),
    re.compile(r"api\.deepinfra\.com"),
    re.compile(r"zenmux\.ai"),
    re.compile(r"api\.minimaxi?\.com"),
    re.compile(r"dashscope\.aliyuncs\.com"),
    re.compile(r"generativelanguage\.googleapis\.com"),
    re.compile(r"api\.deepseek\.com"),
    re.compile(r"api\.perplexity\.ai"),
    re.compile(r"api\.voyageai\.com"),
]

# Known egress doors. Each is capability-gate SHADOW-observed today (WS5 Discord
# shadow-gate) and slated for enforcement in the enforce stage. Additions here MUST
# carry an inline rationale AND route the send through the capability gate.
ALLOWLIST: dict[str, str] = {
    "src/genesis/mcp/discord_bot_mcp.py":
        "external-io-ok: send_reply (Discord API); shadow-observed via observe_discord_send",
    "src/genesis/mcp/outreach_mcp.py":
        "external-io-ok: outreach_poll (Discord webhook); shadow-observed via observe_discord_send",
    "src/genesis/runtime/init/outreach.py":
        "external-io-ok: DiscordWebhookAdapter wiring (reads DISCORD_WEBHOOK_URL); "
        "sends flow through pipeline._deliver, shadow-observed",
    # Test fixtures asserting egress-check behavior must name the endpoints they
    # plant; a NEW egress literal outside these files is still a violation.
    "tests/test_channels/test_discord_adapter.py":
        "external-io-ok: adapter fixture literals",
    "tests/test_hooks/test_secret_scrub.py":
        "external-io-ok: scrub fixtures must carry the strings they scrub",
    "tests/test_mcp/test_capability_shadow_wiring.py":
        "external-io-ok: shadow-gate wiring fixtures",
    "tests/test_mcp/test_outreach_mcp.py":
        "external-io-ok: webhook-env fixtures",
    "tests/test_outreach/test_pipeline.py":
        "external-io-ok: pipeline fixtures",
    "tests/test_scripts/test_check_external_io.py":
        "external-io-ok: this guard's own fixtures",
    "scripts/check_external_io.py":
        "external-io-ok: this script defines the patterns it scans for",
}

# Sanctioned provider references. The rule of thumb: the routing layer, the
# declared provider call sites, and the probes that check those endpoints'
# health/keys — plus test fixtures and the checker itself. A new entry MUST
# name what the file does with the host; "provider-ok: needed" is not a reason.
PROVIDER_ALLOWLIST: dict[str, str] = {
    "src/genesis/routing/litellm_delegate.py":
        "provider-ok: the routing layer itself — the sanctioned call path",
    "src/genesis/channels/stt.py":
        "provider-ok: Groq STT transcription call site",
    "src/genesis/deliberation/backends/fusion.py":
        "provider-ok: OpenRouter fusion backend call site",
    "src/genesis/eval/longmemeval/client.py":
        "provider-ok: eval bench OpenRouter client",
    "src/genesis/memory/embeddings.py":
        "provider-ok: embedding providers (DeepInfra / Dashscope) call sites",
    "src/genesis/memory/reranker.py":
        "provider-ok: Voyage rerank call site",
    "src/genesis/observability/provider_health.py":
        "provider-ok: provider-health probe inventory (read-only /models)",
    "src/genesis/observability/snapshots/api_keys.py":
        "provider-ok: key-validation probe URLs",
    "src/genesis/recon/model_intelligence.py":
        "provider-ok: model metadata fetch (openrouter /models)",
    "src/genesis/research/perplexity.py":
        "provider-ok: Perplexity research channel call site",
    "src/genesis/cc/invoker.py":
        "provider-ok: comment reference only (api.anthropic.com outage note)",
    "scripts/behavioral_linter.py":
        "provider-ok: the linter's own host inventory (_DEGRADED_GATED + docs)",
    "scripts/check_external_io.py":
        "provider-ok: this script defines the patterns it scans for",
    # Test fixtures — the linter's `tests/*` exclusion stands on THIS list
    # being the CI backstop for the same files.
    "tests/test_cc/test_roster.py":
        "provider-fixture: roster routing tests",
    "tests/test_deliberation/test_deliberate.py":
        "provider-fixture: deliberation fixtures",
    "tests/test_hooks/test_behavioral_linter.py":
        "provider-fixture: linter fixtures must contain the hosts it tests",
    "tests/test_hooks/test_inline_hooks.py":
        "provider-fixture: inline-hook fixtures",
    "tests/test_learning/test_procedure_extraction.py":
        "provider-fixture: extraction fixtures",
    "tests/test_memory/test_reranker.py":
        "provider-fixture: reranker fixtures",
    "tests/test_observability/test_key_validator.py":
        "provider-fixture: key-validator fixtures",
    "tests/test_routing/test_provider_health_probes.py":
        "provider-fixture: health-probe fixtures",
    "tests/test_scripts/test_check_external_io.py":
        "provider-fixture: this guard's own fixtures",
}


def scan(
    root: Path,
    allowlist: dict[str, str] | set[str] | None = None,
    patterns: list[re.Pattern[str]] | None = None,
) -> list[tuple[str, int, str]]:
    """Return [(relpath, lineno, line)] for endpoint matches OUTSIDE the allowlist."""
    allowed = set(
        allowlist if allowlist is not None else ALLOWLIST | PROVIDER_ALLOWLIST
    )
    pats = patterns if patterns is not None else (PATTERNS + PROVIDER_PATTERNS)
    violations: list[tuple[str, int, str]] = []
    for path in sorted(root.rglob("*.py")):
        rel = path.as_posix()
        if rel in allowed:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if any(p.search(line) for p in pats):
                violations.append((rel, lineno, line.strip()))
    return violations


def main() -> int:
    violations: list[tuple[str, int, str]] = []
    for root in SCAN_ROOTS:
        if not root.is_dir():
            print(f"external-io guard: scan root {root} not found (run from repo root)")
            return 1
        violations.extend(scan(root))
    if not violations:
        print("External-I/O guard: CLEAN (no ungated egress or provider references "
              "outside the allowlists)")
        return 0
    print("::error::New ungated external reference detected.")
    print("External-world egress: route the send through the capability gate.")
    print("Provider endpoint: route the call through the Genesis routing layer "
          "so it is budgeted and tracked — or add a reasoned ALLOWLIST entry.")
    for rel, lineno, line in violations:
        print(f"  {rel}:{lineno}: {line}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
