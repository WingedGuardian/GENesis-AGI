#!/usr/bin/env python3
"""WS5 external-I/O regression guard — no NEW ungated autonomous external egress.

The whole point of WS5 autonomy-gating is that Genesis must not post to the outside
world (Discord community, public webhooks, public social APIs) through an ungated,
unobserved path. This grep-based guard is the CI/pre-commit backstop: any source file
that references a Discord/webhook/public-social endpoint must be on the ALLOWLIST
(each entry pinned to a known, capability-gated-or-shadowed egress door). A new door
added anywhere else fails the check — forcing it through the capability gate (or an
explicit, reasoned allowlist entry) instead of silently shipping a bypass.

WHAT IT FLAGS (deliberately precise — NOT a generic ``.post(`` scan; there are ~dozens
of legitimate compute/read POSTs: embeddings, search, TTS, crawl, IPC):
  * Any source file that REFERENCES a Discord REST/webhook or public-social (Twitter/X,
    Slack) endpoint or webhook env var (see PATTERNS). A new external door cannot reach
    these services without such a reference at its endpoint lookup, so it trips here.
WHAT IT DOES NOT SEE (by design — this is a reference tripwire, not a taint analysis):
  * A ``.post(pre_built_url)`` whose URL arrives as a variable with no literal endpoint
    token on the call line — e.g. ``channels/discord_adapter.py`` posts to a webhook URL
    it RECEIVES from ``runtime/init/outreach.py``. The guard covers that URL's ORIGIN
    (the DISCORD_WEBHOOK reference in outreach.py, allow-listed) — which is exactly what
    a new door must add — rather than the generic POST site itself.
OUT OF SCOPE (handled elsewhere):
  * Browser-based publishing (e.g. Medium via Playwright) — a different egress modality;
    a browser-egress guard lands with that channel's gating stage.
  * Owner-private notification channels (email-to-owner, Telegram-to-owner) — the owner
    IS the recipient; these are not external-world posting.

Usage:  python scripts/check_external_io.py   (exit 0 = clean, 1 = violation)
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

SCAN_ROOT = Path("src/genesis")

# Endpoint/webhook-env signatures for autonomous external-world HTTP egress.
PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"discord\.com/api"),        # Discord REST API (send_reply)
    re.compile(r"discordapp\.com/api"),     # legacy Discord API
    re.compile(r"DISCORD_WEBHOOK"),         # Discord webhook env (adapter / outreach_poll)
    re.compile(r"api\.twitter\.com"),       # Twitter/X (future public channel)
    re.compile(r"slack\.com/api"),          # Slack API (future)
    re.compile(r"hooks\.slack\.com"),       # Slack webhooks (future)
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
}


def scan(root: Path, allowlist: dict[str, str] | None = None) -> list[tuple[str, int, str]]:
    """Return [(relpath, lineno, line)] for endpoint matches OUTSIDE the allowlist."""
    allowed = set(allowlist if allowlist is not None else ALLOWLIST)
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
            if any(p.search(line) for p in PATTERNS):
                violations.append((rel, lineno, line.strip()))
    return violations


def main() -> int:
    if not SCAN_ROOT.is_dir():
        print(f"external-io guard: scan root {SCAN_ROOT} not found (run from repo root)")
        return 1
    violations = scan(SCAN_ROOT)
    if not violations:
        print("External-I/O guard: CLEAN (no ungated external-egress endpoints outside the allowlist)")
        return 0
    print("::error::New ungated external-world egress detected (WS5 autonomy-gating).")
    print("Route the send through the capability gate, or add a reasoned ALLOWLIST entry.")
    for rel, lineno, line in violations:
        print(f"  {rel}:{lineno}: {line}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
