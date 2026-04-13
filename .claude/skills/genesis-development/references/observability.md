# Genesis Observability Rules

> Expanded reference with code examples and anti-patterns:
> `.claude/docs/observability-rules.md`

## Async & Task Safety

- **Never use bare `asyncio.create_task()`.** Use
  `genesis.util.tasks.tracked_task()`. Tracked tasks have exception
  propagation and heartbeat monitoring.
- **NEVER call `os.killpg()` without validating PGID > 1.**
  `int(AsyncMock().pid)` == 1 in Python 3.12. `os.killpg(1, sig)` ==
  `kill(-1, sig)` == kill ALL user processes in the container. Always set
  `mock_proc.pid = <explicit value>` in tests. Enforced by PreToolUse hook.

## Logging

- **Log operational failures at ERROR, not DEBUG/WARNING.** DB writes, API
  calls, probe failures = ERROR. DEBUG is tracing, WARNING is recoverable
  degradation.
- **Always include `exc_info=True` in error-path logging.** A log message
  without a stack trace is a clue without evidence.
- **Catch specific exceptions before generic.** `except Exception` is last
  resort. Catch `TimeoutError`, `ConnectionError`, `httpx.HTTPStatusError`,
  etc. first with specific messages.

## Suppression Rules

- **Never use `contextlib.suppress(Exception)` in data-returning code.**
  Callers must distinguish "success with zero" from "failed to check."

## Heartbeats & Jobs

- **Every background subsystem must emit heartbeats** via `Severity.DEBUG`
  events.
- **Every scheduled job must record success/failure** via
  `runtime.record_job_success/failure()`.

## Never Hide Problems

**NEVER hide, suppress, or work around broken things — FIX THEM.** This
applies to code, UI, plans, and reasoning. When you encounter something
broken, incomplete, returning "unknown", or showing placeholder data, your
FIRST instinct must be to fix the root cause. Not hide the element. Not
skip the section. Not add a conditional to suppress it. Find the actual
data source (it usually already exists), wire it in, and make it work.
If it genuinely doesn't exist yet, display "not configured" with a clear
explanation — never silently omit.

Enforced by a PreToolUse hook (`scripts/behavioral_linter.py`) that blocks
Write/Edit containing hide-problem patterns in code, plans, or comments.

## Bug Tracking

**Bugs you see get fixed or tracked — never ignored.** Every bug you
encounter during any work (even unrelated work, pre-existing bugs, things
mentioned in passing) must be either fixed inline (if small and low-risk)
or filed as a follow-up (observation, task, TODO) AND raised in your next
user-facing report. "Out of scope" is not an option.
