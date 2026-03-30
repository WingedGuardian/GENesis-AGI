#!/usr/bin/env python3
"""UserPromptSubmit hook: remind about unreviewed code changes.

Fires on every user prompt. If code changes exist without a current review
marker, injects a mandatory reminder into the conversation context.

Silent when:
- No code changes (clean working tree)
- Review marker is current (matches current diff hash)
- Running in a background CC session (GENESIS_CC_SESSION=1)
"""

from __future__ import annotations

import os
import sys

# Skip in background CC sessions
if os.environ.get("GENESIS_CC_SESSION") == "1":
    sys.exit(0)


def main() -> None:
    # Import review_state from same directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    sys.path.insert(0, script_dir)

    try:
        from review_state import has_code_changes, is_review_current
    except ImportError:
        # If review_state.py is missing, don't block — fail open
        sys.exit(0)

    if not has_code_changes():
        sys.exit(0)

    if is_review_current():
        sys.exit(0)

    # Unreviewed changes exist — inject warning
    print(
        "MANDATORY: Unreviewed code changes detected. "
        "You MUST run /review and dispatch the superpowers:code-reviewer agent "
        "before doing any other work or committing. "
        "Commits will be blocked until review is complete.\n\n"
        "ADVERSARIAL REVIEW ORDER: Use Codex (OpenAI) FIRST for adversarial review, "
        "Claude subagent as FALLBACK only. The purpose of adversarial review is a "
        "genuinely independent perspective from a different model — another Claude "
        "instance reviewing Claude's work has blind-spot overlap. Codex catches "
        "things you are architecturally blind to. Do not skip Codex because you are "
        "'already in the flow' — that is exactly when independent review matters most.\n\n"
        "MANDATORY: Before committing, you MUST verify the end-to-end OUTCOME — "
        "not just unit tests. Unit tests prove the code works in isolation. "
        "You must also verify that the actual runtime path delivers the intended "
        "result (e.g., if you wired a notification, confirm it actually sends; "
        "if you fixed a data path, confirm the data actually flows). "
        "Ask: 'If the system restarts now, will this actually work?' "
        "If you cannot answer yes WITH EVIDENCE, you are not done."
    )


if __name__ == "__main__":
    main()
