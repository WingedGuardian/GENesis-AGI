#!/usr/bin/env python3
"""PostToolUse hook (browser_navigate): point at the browser skills.

Fires after browser_navigate on ANY browser layer (Camoufox, Chromium, remote
CDP, TinyFish) with the safety pointer: payments and credentials follow the
Safety gates of the browser-automation skill. On Camoufox, the default
stealth layer, it also points at the stealth-browser skill for anti-detection
behaviour.

Each pointer fires once per session (its own sentinel file), so a session that
starts on Chromium and later moves to Camoufox still gets the stealth pointer.
With no usable session id the pointers repeat on every navigate instead.

Never blocks (exit 0 always). Advisory only.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile

# Self-locate so hook_input resolves whether run as a script or imported (tests).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hook_input import read_payload, session_id, tool_response  # noqa: E402

_SENTINEL_PREFIX = "genesis_stealth_nudge_"
_SAFETY_SENTINEL_PREFIX = "genesis_browser_safety_nudge_"

_SAFETY_NUDGE = (
    "Browser tools are active. Before any purchase or payment, and before "
    "typing a password, code or card number, follow the Safety gates in the "
    "browser-automation skill (`src/genesis/skills/browser-automation/SKILL.md`): "
    "explicit approval for each transaction, never type a card number, never "
    "type a password or code pasted into chat, credentials for agent-owned "
    "accounts only from reference_lookup, and banking is handed to the user."
)

_STEALTH_NUDGE = (
    "Camoufox stealth browser is active. Load the stealth-browser skill "
    "(`src/genesis/skills/stealth-browser/SKILL.md`) for anti-detection "
    "behaviour: what the tools already do, page warm-up, honeypot checks, "
    "fill order, and how Cloudflare Turnstile is handled."
)


def _session_sentinel_path(sid: str, prefix: str = _SENTINEL_PREFIX) -> str:
    """Path to a sentinel file that tracks whether we've nudged this session."""
    return os.path.join(tempfile.gettempdir(), f"{prefix}{sid}")


def _claim(sid: str, prefix: str) -> bool:
    """True the first time this session reaches the ``prefix`` sentinel; records it.

    With no usable session id there is no per-session key, and a shared one
    would let the first such session silence the pointer for every later one,
    so the pointer repeats instead and nothing is written.
    """
    if not sid:
        return True
    path = _session_sentinel_path(sid, prefix)
    if os.path.exists(path):
        return False
    try:
        with open(path, "w") as f:
            f.write("1")
    except OSError:
        pass  # Non-critical: worst case the pointer repeats
    return True


def main() -> int:
    payload = read_payload()
    sid = session_id(payload, default="")

    try:
        result = tool_response(payload)
        if not result:
            return 0

        layer = result.get("layer", "")
        if not layer:
            return 0  # a failed navigate (error dict) carries no layer

        parts = []
        if _claim(sid, _SAFETY_SENTINEL_PREFIX):
            parts.append(_SAFETY_NUDGE)
        if layer == "camoufox" and _claim(sid, _SENTINEL_PREFIX):
            parts.append(_STEALTH_NUDGE)
        if not parts:
            return 0

        # Reaches the model ONLY via hookSpecificOutput.additionalContext;
        # a top-level additionalContext key is silently discarded.
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": "\n\n".join(parts),
                    }
                }
            )
        )

    except (json.JSONDecodeError, KeyError, AttributeError):
        pass  # Fail-open

    return 0


if __name__ == "__main__":
    sys.exit(main())
