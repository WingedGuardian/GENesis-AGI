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

    # Unreviewed changes exist — inject the base reminder UNCONDITIONALLY first.
    # The deterministic review-scope manifest below is strictly ADDITIVE: it is
    # built and appended behind its own guards so a manifest error can never
    # truncate or suppress this core reminder.
    print(
        "MANDATORY: Unreviewed code changes detected. "
        "You MUST run /review and dispatch the superpowers:code-reviewer agent "
        "before doing any other work or committing. "
        "Commits will be blocked until review is complete.\n\n"
        "TWO DIFFERENT THINGS, do not conflate them:\n"
        "1. REVIEWING YOUR OWN WORK (what this reminder is about). You have "
        "UNCOMMITTED changes; the commit gate wants evidence you reviewed them. This "
        "is UNRESTRICTED — dispatch whatever reviewer or subagent helps, as many as "
        "help, no approval needed. Then record the evidence and commit.\n"
        "2. THE CROSS-MODEL GATE, which lives on the PR AFTER you push. Its purpose "
        "is a genuinely independent perspective from a different model: another Claude "
        "instance reviewing Claude's work has blind-spot overlap, and Codex catches "
        "things you are architecturally blind to. Codex is that gate, and it is the "
        "ONLY reviewer you may run without asking.\n"
        "ANY OTHER AGENT STANDING IN FOR CODEX AT THE GATE REQUIRES THE USER'S EXPLICIT "
        "APPROVAL, EVERY TIME — whatever it is, however it is invoked. Approval never "
        "carries forward to the next use. Identifying the correct route is NOT "
        "permission to take it: work out which reviewer applies, then ASK for it. A "
        "clean review from an agent you were not authorised to run does not satisfy "
        "the gate.\n"
        "TWO CODEX SURFACES, judged separately: the GitHub PR reviewer ('@codex review' "
        "on the PR) and the local 'codex exec' CLI. Do not infer one's availability from "
        "the other — establish each on its own surface, asked now. OBSERVED once "
        "(2026-08-27): the CLI reported a two-week lockout while the GitHub reviewer "
        "returned a full review on the same commit minutes later; the cause (separate "
        "metering, plan tier, or a CLI-side fault) was not established. Note the GitHub "
        "reviewer structurally CANNOT see an uncommitted diff — it reviews a PR head — "
        "so its being unavailable pre-commit is not a licence to substitute another "
        "agent at the gate; it just means the gate has not been reached yet.\n\n"
        "MANDATORY: Before committing, you MUST verify the end-to-end OUTCOME — "
        "not just unit tests. Unit tests prove the code works in isolation. "
        "You must also verify that the actual runtime path delivers the intended "
        "result (e.g., if you wired a notification, confirm it actually sends; "
        "if you fixed a data path, confirm the data actually flows). "
        "Ask: 'If the system restarts now, will this actually work?' "
        "If you cannot answer yes WITH EVIDENCE, you are not done.",
        flush=True,  # flush BEFORE the manifest's git calls: hook stdout is
        # block-buffered (piped), so if the manifest git work overruns the 10s
        # hook timeout and Python is killed, the base reminder must already be out.
    )

    # Additive: deterministic per-file review-scope manifest. Fully fail-open —
    # any import/build error is swallowed so the base reminder above stands alone.
    # build_manifest self-bounds its total git time under the hook timeout.
    try:
        from review_scope import build_manifest, render_reminder_block

        block = render_reminder_block(build_manifest())
        if block:
            print("\n" + block)
    except Exception:  # noqa: BLE001 - manifest is best-effort, never load-bearing
        pass


if __name__ == "__main__":
    main()
