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
        "MANDATORY: Unreviewed code changes detected. Review the BRANCH CHANGESET — "
        "`git diff $(git merge-base HEAD <your default branch>)`, which covers committed, "
        "staged and unstaged work (but NOT untracked files — `git status` for those). "
        "Do not hardcode `origin/main`: where the default branch is named otherwise the "
        "substitution collapses and `git diff` silently reviews UNSTAGED work only, exit "
        "0. The scope manifest below, when present, names the base it resolved. Reviewing "
        "only the index leaves earlier commits on this branch uncovered. The MARKER, "
        "separately, binds to what is STAGED at mark time: review the branch, mark after "
        "staging.\n"
        "A docs/config-only staged set needs NO review and NO marker — but the exemption "
        "is NARROWER than it sounds and this reminder cannot tell (it hashes the staged "
        "diff without classifying it). It does NOT apply when: the commit command itself "
        "stages (`git add …&& git commit`, `git commit -am`, a pathspec, `-a/-i/-o/-p`); "
        "you are at review round 2 or 3, where the round caps are evaluated FIRST; the "
        "set touches anything under a `.github/` directory (workflows are executable CI "
        "config with repo secrets); or it touches a prompt / agent / skill SURFACE under "
        "`.claude/{agents,commands,skills}/` or `src/genesis/{skills,identity,**/prompts}/` "
        "even when `.md`. Note `docs/config` is an EXTENSION allowlist (.md .rst .txt "
        ".yaml .yml .toml .ini .cfg) — `.json`, `Makefile`, `.sh` and extensionless files "
        "are code. Root SOUL.md / CLAUDE.md / USER.md are deliberately exempt.\n\n"
        "HOW TO CLEAR IT — PLUGIN ROUTES, where this install has them: `/review`, "
        "`/code-review`, or the `superpowers` skills `superpowers:requesting-code-review` "
        "/ `superpowers:receiving-code-review`. Plugin availability is PER INSTALL and "
        "varies between machines, so CHECK what resolves here rather than assuming either "
        "way; an absent name simply does not resolve and you use the built-ins below. "
        "ALWAYS AVAILABLE: `/deep-review` does the whole flow, review through marker; or "
        "by hand, write findings to `python3 scripts/review_state.py evidence-path`, then "
        "`python3 scripts/review_state.py mark --agent-output <that file>` — a plain "
        "mark is an INTERNAL (same-model) review: it satisfies this gate and NEVER "
        "counts toward the escalation streak, so no outcome flag is needed. That is the "
        "common case (a genesis-architect / genesis-security / subagent audit — free, "
        "and it shares this model's blind spots, so it must not penalize you). EXTERNAL is "
        "judged by the reviewing MODEL, not the gateway: only a review by a non-ANTHROPIC "
        "model counts. Anthropic Claude via ANY route (incl. an OpenRouter Claude route) is "
        "INTERNAL, and Genesis's own cognitive/routing systems are never reviewers (they are "
        "infrastructure, not a review service). Approved external methods TODAY are Codex and "
        "Kimi (on .123); OpenRouter is NOT an approved method today. Mark such a review "
        "`--source external`, and that one alone MOVES the streak, so it requires an "
        "outcome: `--defects` for a NEW should-fix-or-worse finding, or `--clean` for "
        "none (which RESETS the streak). An internal self-review can never reset a "
        "standing cross-model streak — only an external `--clean` round does. "
        "Should-fix-or-worse is PER REVIEWER VOCABULARY: genesis-architect -> "
        "BLOCKER/SHOULD-FIX; genesis-security-reviewer -> CRITICAL/WARNING (WARNING is "
        "its should-address tier, NOT a note); Codex -> P1/P2. WRITING the evidence is a "
        "SEPARATE vocabulary: the validator recognises only BLOCKER / SHOULD-FIX / "
        "CRITICAL / HIGH / MEDIUM / LOW / P1-P3, uppercase — `WARNING` and Title-Case "
        "tiers like `Important` are NOT recognised, so render them as SHOULD-FIX in the "
        "text or the depth gate rejects the evidence as non-adversarial. "
        "RUN BOTH FROM THE WORKTREE THE CHANGES ARE IN — the marker is keyed "
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
        "(`# escalation-ack`), which DOES reset the streak — so that cycle repeats. "
        "A second counter does not reset: at 7 EXTERNAL rounds over the branch's "
        "whole life the gate reaches its TERMINAL, where `# escalation-ack` no "
        "longer helps and the only ways out are to accept the outstanding findings "
        "and merge (`# final-round-accept`, which clears ONE commit and then blocks "
        "again) or to abandon the branch and restart.\n\n"
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
