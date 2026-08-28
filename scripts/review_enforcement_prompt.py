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
        "ADVERSARIAL REVIEW ORDER: Use Codex (OpenAI) FIRST for adversarial review, "
        "Claude subagent as FALLBACK only. The purpose of adversarial review is a "
        "genuinely independent perspective from a different model — another Claude "
        "instance reviewing Claude's work has blind-spot overlap. Codex catches "
        "things you are architecturally blind to. Do not skip Codex because you are "
        "'already in the flow' — that is exactly when independent review matters most.\n"
        "TWO CODEX SURFACES, judged separately: the GitHub PR reviewer ('@codex review' "
        "on the PR) and the local 'codex exec' CLI. Do not infer one's availability from "
        "the other — a CLI 'usage limit' error is NOT grounds to fall back to a Claude "
        "reviewer WHEN THE GITHUB PATH EXISTS; try it first, and vice versa. OBSERVED "
        "once (2026-08-27): the CLI reported a two-week lockout while the GitHub reviewer "
        "returned a full review on the same commit minutes later; the cause (separate "
        "metering, plan tier, or a CLI-side fault) was not established.\n"
        "PRE-COMMIT DIFFS ARE THE EXCEPTION, and it is not optional. Changes that exist "
        "only in the working tree are at no PR head, so the GitHub reviewer CANNOT see "
        "them — while the commit gate blocks the very commit that would publish them "
        "until a review marker exists. Insisting on the GitHub path there leaves NO "
        "compliant route when the CLI is quota-limited, and the only exit is an override "
        "— worse than the fallback the rule was written to prevent. So for an uncommitted "
        "diff with the CLI unavailable, the documented Claude adversarial fallback IS the "
        "correct route; record in the review evidence that Codex was unavailable and how "
        "that was established.\n\n"
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
