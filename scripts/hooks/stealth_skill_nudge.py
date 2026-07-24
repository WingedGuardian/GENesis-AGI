#!/usr/bin/env python3
"""PostToolUse hook (browser_navigate): nudge stealth-browser skill.

Fires after browser_navigate is called with Camoufox (the default).
Reminds the session to load the stealth-browser skill for anti-detection
behavioral rules and the VNC trusted input technique.

Never blocks (exit 0 always). Nudges once per session via sentinel file.
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


def _session_sentinel_path(sid: str) -> str:
    """Path to a sentinel file that tracks whether we've nudged this session."""
    return os.path.join(tempfile.gettempdir(), f"{_SENTINEL_PREFIX}{sid}")


def main() -> int:
    payload = read_payload()

    # Only nudge once per session
    sentinel = _session_sentinel_path(session_id(payload))
    if os.path.exists(sentinel):
        return 0

    try:
        result = tool_response(payload)
        if not result:
            return 0

        layer = result.get("layer", "")

        # Only nudge for Camoufox (stealth) sessions, not Playwright/Chromium
        if layer != "camoufox":
            return 0

        # Create sentinel — we've nudged
        try:
            with open(sentinel, "w") as f:
                f.write("1")
        except OSError:
            pass  # Non-critical

        nudge = (
            "Camoufox stealth browser is active. Load the stealth-browser skill "
            "(`src/genesis/skills/stealth-browser/SKILL.md`) for anti-detection "
            "behavioral rules: page warm-up, honeypot detection, form fill order, "
            "timing profiles, and the VNC trusted input technique for bypassing "
            "Cloudflare Turnstile checkboxes."
        )
        print(json.dumps({"additionalContext": nudge}))

    except (json.JSONDecodeError, KeyError):
        pass  # Fail-open

    return 0


if __name__ == "__main__":
    sys.exit(main())
