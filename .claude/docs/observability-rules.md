# Observability Rules — Expanded Reference

This expands on the observability rules in CLAUDE.md with code examples,
anti-patterns, and the incidents that created each rule.

## Rule: Never use bare `asyncio.create_task()`

**Use `genesis.util.tasks.tracked_task()` instead.**

Bare `create_task` loses exceptions silently. `tracked_task` logs failures,
emits events, and prevents silent death of background work.

```python
# BAD
asyncio.create_task(do_work())

# GOOD
from genesis.util.tasks import tracked_task
tracked_task(do_work(), name="my-work")
```

## Rule: Never use `contextlib.suppress(Exception)` in data-returning code

Callers must distinguish "success with zero" from "failed to check."
Suppressing exceptions in data paths creates silent data loss.

## Rule: Log at ERROR, not DEBUG/WARNING

DB writes, API calls, probe failures = ERROR. These are operational failures
that need attention. DEBUG is tracing. WARNING is recoverable degradation.

```python
# BAD
logger.debug("Failed to write event: %s", e)

# GOOD
logger.error("Failed to write event to genesis.db", exc_info=True)
```

## Rule: Always include `exc_info=True`

A log message without a stack trace is a clue without evidence. Every
error-path log must include `exc_info=True`.

## Rule: Catch specific exceptions first

`except Exception` is last resort. Catch `TimeoutError`, `ConnectionError`,
`httpx.HTTPStatusError`, etc. first with specific recovery logic.

```python
# BAD
except Exception as e:
    logger.error("Something failed: %s", e)

# GOOD
except httpx.HTTPStatusError as e:
    logger.error("API returned %d for %s", e.response.status_code, url, exc_info=True)
except TimeoutError:
    logger.error("Request to %s timed out after %ds", url, timeout, exc_info=True)
except Exception as e:
    logger.error("Unexpected error calling %s", url, exc_info=True)
```

## Rule: Every background subsystem must emit heartbeats

Via `Severity.DEBUG` events. If a subsystem stops emitting heartbeats, the
health dashboard shows it as stale. This is how we detect dead event loops.

## Rule: Every scheduled job must record success/failure

Via `runtime.record_job_success()` / `runtime.record_job_failure()`. The job
health dashboard depends on these records.

## Rule: NEVER call `os.killpg()` without validating PGID > 1

`int(AsyncMock().pid)` == 1 in Python 3.12. `os.killpg(1, sig)` ==
`kill(-1, sig)` == kill ALL user processes in the container. This happened
on 2026-03-16 and killed the entire container. Always set
`mock_proc.pid = <explicit value>` in tests.

## Rule: NEVER hide broken things — FIX THEM

This is the most important observability rule because it's a thinking rule,
not just a code rule. When you encounter something broken, incomplete, or
showing placeholder data, your FIRST instinct must be to fix the root cause.
Not hide the element. Not skip the section. Not add a conditional to suppress
it. Find the actual data source and wire it in.

Enforced by `scripts/behavioral_linter.py` PreToolUse hook.
