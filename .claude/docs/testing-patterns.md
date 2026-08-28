# Genesis Testing Patterns

## Tools

- **pytest** with `pytest-asyncio` (asyncio_mode = auto)
- **ruff** for linting (config in `pyproject.toml`)
- Run both before every commit: `ruff check . && pytest -v`

## File Layout

```
tests/
├── __init__.py
├── test_smoke.py          # Harness verification
├── test_db/               # Phase 0: schema + CRUD tests
│   ├── test_memory.py
│   ├── test_observations.py
│   └── ...
├── test_mcp/              # MCP server interface tests
│   ├── test_memory_mcp.py
│   └── ...
└── conftest.py            # Shared fixtures (db connections, test data)
```

## Naming

- Files: `test_<module>.py`
- Functions: `test_<behavior>` — describe WHAT is being verified, not HOW
- Good: `test_store_episodic_memory_returns_id`
- Bad: `test_insert` or `test_memory_1`

## What to Test

- **Every CRUD operation**: create, read, update, delete
- **Edge cases**: empty inputs, missing fields, duplicate keys, max-length strings
- **Failure paths**: not just the happy path. What happens when the DB is locked?
  When a required field is None? When a foreign key doesn't exist?
- **Schema constraints**: verify NOT NULL, UNIQUE, CHECK constraints reject bad data

## What NOT to Test

- Don't test SQLite itself (it works)
- Don't test third-party libraries (they have their own tests)
- Don't write tests for code that doesn't exist yet

## Fixtures

Use `conftest.py` for shared setup:
- In-memory SQLite (`":memory:"`) for fast, isolated DB tests
- Factory functions for creating test data (not raw INSERT statements)
- Async fixtures for MCP server tests

## One Test Run at a Time (box-wide lock)

Concurrent suites contend for the CPU and RAM of the live Genesis services on a
swapless box and take far longer than running in turn, so pytest runs are
serialized across the whole machine.

**One layer decides concurrency: the lock.** `genesis.util.pytest_lock` holds a non-blocking
`flock` on `~/.genesis/locks/pytest.lock` for exactly the duration of a run. The
kernel releases it on process death, SIGKILL included, so a crashed run never
wedges the box.

Two kinds of caller acquire it:

- `tests/conftest.py::pytest_configure` — every run of *this* repo's suite, from
  any launcher and any worktree, because nothing reaches those tests without
  loading that conftest.
- `genesis.eval.gauntlet` — for its **scoring** subprocess, which runs a foreign
  fixture project with its own rootdir where this conftest never loads. The
  agent's own `python -m pytest` iterations while solving a fixture are *not*
  covered: holding the lock across a 15–20 minute agent session would starve
  every interactive run for longer than the overlap it prevents. That boundary
  is deliberate.

`scripts/pytest_lock_wait.py` is a **pure CLI** that only asks the lock "free
yet?". It decides nothing.

> There used to be a second layer — a PreToolUse hook that blocked Bash pytest
> calls on its own reading of the command line. Two layers interpreting
> different things (a shell command vs. an environment) drifted apart five
> times across three review rounds: the hook refused the very overrides the
> lock's message prescribed, read an env prefix on an *unrelated* shell segment
> as an opt-out, and disagreed with the lock about unrecognised values and about
> the lock path. It was deleted rather than patched. If you are tempted to add
> **concurrency** allow/deny logic outside the lock, that is the class you are
> re-opening.
>
> `full_suite_guard` is still a PreToolUse/Bash hook that refuses pytest — but on
> a different axis (scope: whole-directory vs targeted), with its own
> `# full-suite-ok` override and its own exit status. Two layers deciding
> *different* questions is fine; two deciding the *same* one is what cost five
> defects.

### When a run is refused

The message names the holder's pid, command and age, plus how to wait:

```bash
python3 scripts/pytest_lock_wait.py            # default: 2h ceiling
python3 scripts/pytest_lock_wait.py --wait=600  # or an explicit bound
```

The refusal prints an **absolute** path to that script, because the lock governs
runs from any working directory — a repo-relative command breaks the moment
pytest is launched from a subdirectory.

Never hand-roll a waiter. A `pgrep`-based loop matches the waiter's own
`bash -c` argv — the pattern it searches for is right there in its own command
line — so it waits on itself forever, and it matches other sessions' waiters too.

### Levers

| Variable | Effect |
| --- | --- |
| `GENESIS_PYTEST_LOCK=0` | Disable the lock — a deliberate concurrent run. |
| `GENESIS_PYTEST_LOCK_WAIT=1` | Queue until free instead of failing fast. The right shape for a background eval. |
| `GENESIS_PYTEST_LOCK_WAIT_TIMEOUT=<s>` | Bound on that wait (default 7200, capped 86400). |
| `GENESIS_PYTEST_LOCK_PATH=<file>` | Point the lock at an explicit file instead of the default. |

The lock fails **open** by contract: a missing `~/.genesis`, an unreadable lock
directory, an unrecognised value for one of those variables, or any unexpected
error means the tests run. It is a resource governor, not a correctness gate, and
a fault in it must never be able to stop the suite. Introspection-only modes
(`--help`, `--fixtures`, `--markers`, `--version`) are never blocked: they run no
tests, and pytest calls `_do_configure()` outside its session wrapper for them,
so refusing there surfaces as a traceback rather than a clean exit.

A run refused for contention exits **200**, deliberately not 1 — that is "tests
failed", and conflating the two would send you debugging.

## Before Claiming "Tests Pass"

Run the actual command and read the output. Don't guess.
```bash
source ~/genesis/.venv/bin/activate
cd ~/genesis && ruff check . && pytest -v
```
