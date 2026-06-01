#!/usr/bin/env python3
"""DEPRECATED: Logic merged into genesis_stop_hook.py (2026-06-01).

This file had two issues:
1. It was never registered in settings.json (built but not wired).
2. It read the wrong field name ('stop_response' instead of
   'last_assistant_message'), so it would have silently failed even if wired.

The outcome verification logic now lives in genesis_stop_hook.py alongside
the giving-up detection and review enforcement checks.

Original description:
Stop hook: remind to verify actual outcomes before claiming done.

When the assistant's response contains finishing-stage language (merge options,
PR creation, "implementation complete") but lacks evidence of integration or
e2e verification beyond unit tests, inject a reminder.

Reads hook input from stdin as JSON:
  {"session_id": "...", "stop_response": "...", ...}

Skips background sessions (GENESIS_CC_SESSION=1).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

_FLAG = Path.home() / ".genesis" / "cc_context_enabled"

# Patterns indicating the assistant is at the "finishing" stage
_FINISHING_PATTERNS = re.compile(
    r"(?:"
    r"[Mm]erge\s+(?:back\s+)?to\s+main"
    r"|[Cc]reate\s+a\s+[Pp]ull\s+[Rr]equest"
    r"|[Pp]ush\s+and\s+create"
    r"|[Ii]mplementation\s+complete"
    r"|What\s+would\s+you\s+like\s+to\s+do\?"
    r"|Keep\s+the\s+branch\s+as-is"
    r"|Discard\s+this\s+work"
    r")",
)

# Patterns indicating integration/e2e verification was actually done
_VERIFICATION_EVIDENCE = re.compile(
    r"(?:"
    r"[Ii]ntegration\s+test"
    r"|[Ee]2[Ee]\s+test"
    r"|[Ss]moke\s+test"
    r"|[Aa][Pp][Ii]\s+(?:smoke\s+)?test"
    r"|[Vv]erif(?:y|ied)\s+(?:the\s+)?(?:actual|end-to-end|e2e)"
    r"|[Mm]anual(?:ly)?\s+(?:test|verif)"
    r"|[Ll]ive\s+(?:test|verif)"
    r"|[Tt]elegram\s+API\s+(?:smoke|confirmed|test)"
    r"|[Aa]syncio\s+integration"
    r"|[Pp]roduction\s+(?:test|verif)"
    r"|[Oo]utcome\s+verif"
    r")",
)


def main() -> None:
    if not _FLAG.exists():
        return

    if os.environ.get("GENESIS_CC_SESSION") == "1":
        return

    try:
        raw = sys.stdin.read()
        hook_input = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, OSError):
        return

    # The stop hook receives the assistant's response
    assistant_msg = hook_input.get("stop_response", "")
    if not assistant_msg:
        return

    # Check if this looks like a finishing turn
    if not _FINISHING_PATTERNS.search(assistant_msg):
        return

    # Check if verification evidence is present
    if _VERIFICATION_EVIDENCE.search(assistant_msg):
        return

    # Finishing language present, but no verification evidence
    print(
        "OUTCOME VERIFICATION REMINDER: You're presenting completion options "
        "but haven't mentioned integration or e2e verification beyond unit tests. "
        "Before the user decides, verify the actual outcome works — API smoke test, "
        "live verification, or whatever proves this will work in production. "
        "Don't just run pytest and claim done."
    )


if __name__ == "__main__":
    main()
