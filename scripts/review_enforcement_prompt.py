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
        "MANDATORY: Unreviewed code changes detected. Review what is STAGED, then "
        "record evidence — a code commit is blocked until a review marker matches the "
        "staged diff (a docs/config-only commit is not).\n\n"
        "HOW TO CLEAR IT — PREFERRED, where the install has it: the `superpowers` "
        "plugin (`/review`, `superpowers:code-reviewer`). It is optional and NOT "
        "installed everywhere, so check before relying on it; if it is absent those "
        "names simply do not resolve and you use the built-ins below. ALWAYS "
        "AVAILABLE: `/deep-review` does the whole flow, review through marker; or by "
        "hand, write findings to `python3 scripts/review_state.py evidence-path`, then "
        "`python3 scripts/review_state.py mark --agent-output <that file>` (add "
        "`--clean` when the round found nothing, or it counts as a defect-bearing "
        "round). RUN BOTH FROM THE WORKTREE THE CHANGES ARE IN — the marker is keyed "
        "by process cwd, not by how you spell the path.\n"
        "The marker binds to what is staged AT MARK TIME and sees STAGED changes only. "
        "So mark AFTER staging, and if a reviewer's finding made you edit, re-inspect "
        "and make the evidence describe the FINAL staged state — otherwise evidence "
        "written before the fix authorises code nobody reviewed.\n"
        "A SUBSTANTIAL change (>=50 reviewable lines, >1 code file, or any auth / API / "
        "migration / prompt / agent / skill surface) additionally needs evidence that is "
        "adversarial in STRUCTURE — a severity ladder, `file:line` references, and real "
        "substance — or the depth gate blocks the commit whatever you ran.\n"
        "ONE REVIEWER AT A TIME — never two review agents in parallel (standing user "
        "directive). The second should see the FIXED code, not the same unfixed diff.\n"
        "ROUNDS ARE CAPPED IN TWO TIERS, and the one you hit FIRST is the one sessions "
        "forget: at round 2 a MODE-SWITCH block demands you stop patching the named "
        "instance, audit the ENTIRE diff with a fresh-context reviewer, and fix the "
        "whole CLASS in one commit (`# audit-ack`; the counter does NOT reset). At "
        "round 3 it is a HARD STOP needing a fresh decision from the user "
        "(`# escalation-ack`).\n\n"
        "THE CROSS-MODEL GATE IS A DIFFERENT THING, and it lives on the PR AFTER you "
        "push — nothing about it is actionable here. Its point is a perspective from a "
        "different model: another Claude reviewing Claude's work shares its blind "
        "spots. Codex is that gate; ANY other agent standing in for it needs the "
        "user's explicit approval EVERY time, and identifying the right route is not "
        "permission to take it. Do not request an at-head review before the head you "
        "want reviewed is pushed. Details, and the rest of the merge gate (CI, base, "
        "Codex freshness, scheduled review, hook-surface evidence), are in the "
        "genesis-development skill — consult it there rather than from memory.\n\n"
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
