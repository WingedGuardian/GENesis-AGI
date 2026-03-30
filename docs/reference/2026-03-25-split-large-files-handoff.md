# Split Large Files Recovery Handoff

Date: 2026-03-25
Branch merged from: `refactor/split-large-files-v2`

## What Was Fixed

- Restored `genesis.observability.health_data.CC_JSONL_DIR` for compatibility.
- Repaired `memory_mcp` compatibility so package splits preserve live module state and monkeypatch surfaces.
- Converted `genesis.mcp.health_mcp` into a real compatibility shim over `genesis.mcp.health`.
- Restored awareness reflection dispatch so slow reflection work runs outside the tick lock.
- Restored surplus stuck-task retry semantics.
- Restored provider activity DB fallback visibility and `window_hours`.
- Fixed watchdog path/reclaim compatibility in worktree contexts.
- Restored UI memory stats empty-state schema.
- Removed unrelated README scope creep from the split branch.

## Validation Result

- Full split-worktree test gate passed:
  - `3767 passed`
  - `50 warnings`
- Edited files passed `ruff check`.
- Post-merge validation on the integrated checkout branch also passed:
  - key integrated regression slice: `137 passed`
  - runtime/reflection/integration slice: `88 passed, 1 warning`
  - dashboard/observability/surplus/MCP/telegram slice: `336 passed, 8 warnings`
  - key integrated files passed `ruff check`

## Landing Status

- The validated merge result currently lives on branch:
  - `integration/split-large-files-main-merge`
- It is not yet committed onto `main`.
- Direct commit to `main` was blocked by the repo's active-worktree safety policy.
- To make this live on anything that deploys from `main`, this integration branch still needs to be merged or fast-forwarded onto `main` after clearing that policy constraint.

## Outstanding Issues

- Full-repo `ruff check` is still not globally clean because the branch already contains unrelated lint debt outside the files changed in this recovery.
- The full test suite still emits pre-existing warnings, including:
  - `PytestUnknownMarkWarning` for `slow`
  - several `AsyncMock`/unawaited coroutine warnings in older tests
  - PTB deprecation warnings from telegram dependencies
- These warnings were present in the final green run and should be cleaned separately from this merge.

## Suggested Follow-Up

- Run a repo-wide lint cleanup in a separate branch.
- Triage and reduce the remaining async warning noise so future regressions are easier to spot.
- Once this is on `main`, watch the first real runtime/bootstrap cycle closely:
  - awareness tick freshness
  - dashboard heartbeat visibility
  - MCP health/memory tool imports
  - surplus stuck-task recovery behavior
