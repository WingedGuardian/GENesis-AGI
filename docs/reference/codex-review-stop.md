# Codex review-budget stop and handoff

The project `.codex/config.toml` wires a local CLI `PreToolUse` shell hook. It
reuses Genesis's review-request and commit-budget decisions. Within budget the
action proceeds; a decision requiring approval, a denial, or unreadable evidence
stops execution and asks the agent to return to the user.

This adapter cannot obtain approval. On Codex CLI 0.159.2, a hook returning
`ask`, malformed JSON, or an uncaught exception was measured to permit execution.
This adapter emits no approval JSON: denial is exit code 2. Its shell launcher
requires an explicit allow result and converts missing/malformed results and
evaluator/import failures into exit 2, with an outer configuration
fallback if the script cannot launch. This does not guarantee denial when the
outer hook command itself cannot start or is killed.

## Activation

Verified with local CLI 0.159.2; re-run the probes for another version before
claiming support. Use a local CLI with hooks enabled. Trust this project's configuration and
inspect/approve its hook through the client's hook-trust flow. Start a new
session after updating. Disabled, untrusted or unsupported hooks supply no
protection. This is not an assertion about hosted/cloud orchestration.

Codex CLI 0.159.2 takes linked-worktree hook declarations from the primary
checkout, replacing worktree-local declarations. Updating only a linked worktree
does not activate a new hook. After landing, update the primary checkout and
trust its hook before starting another session. The command then resolves scripts
from the current git checkout; reconcile old branches before relying on their
policy. No Genesis server restart is needed. This behavior was checked against
the [configuration loader](https://github.com/openai/codex/blob/main/codex-rs/config/src/loader/mod.rs)
and reproduced with harmless CLI probes: regular-checkout configuration blocked
an exhausted request, while declarations present only in a linked worktree did not.

## Protected actions and limits

The existing request guard recognizes shell review requests; the shared parser
recognizes `git commit`. Public-repository scope, no-open-PR exemptions, standing
limits and gate-surface marked confirmation remain the shared evaluator's rules.
The adapter does not define another counter or policy. Ordinary read-only shell
commands perform no cloud budget lookup.

One commit must run separately from other git/gh operations. Literal `cd` and
`git -C` can select its directory. Unknown directories, repository overrides,
unresolvable nested commits and unreadable relevant syntax are denied. This
budget-only adapter adds no review-current marker, depth, push or merge gates.

This is accident prevention for recognized shell actions. It does not inspect
arbitrary Python programs, binaries or scripts that perform actions internally,
arbitrary MCP calls, or later `write_stdin` input. Deliberate hook disabling is
also outside its protection claim. It is not a universal enforcement boundary.

## Handoff

When blocked, stop and present the reason to the user. Earlier “continue” does
not authorize another action. Do not add a sigil, persist an approval file,
disable hooks, or retry through another tool. The user may choose merge with
accepted issues, rework, or further work in a client with a supported fresh native
approval boundary. That client's decision cannot become a reusable Codex receipt.

Codex remains external: this action hook does not register a transcript/session,
create a charter, or add Genesis lifecycle management.

## Interaction with the review-gate series

The adapter intentionally consumes private budget helpers in `git_push_guard`
and `review_enforcement_commit` rather than extracting another policy layer.
Changes to those helpers must run the adapter's integration tests too.

PR #2683 owns reviewer identities and parser extraction; this adapter changes
none of its files and copies no reviewer login. The reflection series owns round
definition, reflection gates, scoring, provenance and CI. Its future commit/edit
reflection gates are not automatically enforced by this adapter: test that
integration when the common decision seam lands. CI can prevent merging across
harnesses, but cannot prevent local edits or review dispatch before execution.
Issue #2166 remains open for cross-agent approval and broader enforcement.

## Verification

Run `pytest tests/test_hooks/test_codex_review_stop.py` plus the shared request
and budget suites. Before trusting another CLI version, use harmless fake git/gh
executables, fixture PR evidence and the actual hook in an isolated directory.
A below-budget control must execute; exhausted-budget and evaluator-failure
cases must not. Observe command execution events and fake action receipts,
not the model's final answer or CLI exit status. Repeat E2E after final review
changes and on the merged checkout. Never trigger a real review or commit for
the probe.

Contract: [official OpenAI hooks documentation](https://learn.chatgpt.com/docs/hooks),
checked 2026-09-30. Local tool coverage and unsupported `ask` are explicit.
