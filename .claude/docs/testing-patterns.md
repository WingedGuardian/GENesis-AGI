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

**The lock is the single source of truth.** `genesis.util.pytest_lock` holds a
non-blocking `flock` on `~/.genesis/locks/pytest.lock` for exactly the duration
of a run. The kernel releases it on process death, SIGKILL included, so a
crashed run never wedges the box.

Two kinds of caller acquire it:

- `tests/conftest.py::pytest_configure` — every run of *this* repo's suite, from
  any launcher and any worktree, because nothing reaches those tests without
  loading that conftest.
- `genesis.eval.gauntlet` — explicitly, because it scores *foreign* fixture
  projects: it runs pytest with a `cwd` under `~/tmp/gauntlet`, so those
  projects have their own rootdir and this repo's conftest never loads for them.

`scripts/hooks/concurrent_test_guard.py` is a PreToolUse hook that gives fast
feedback *before* pytest starts collecting. It does not decide anything on its
own — it **probes the same lock**. An earlier design had it scan `/proc` argv
instead, which meant two oracles that could disagree: a holder spelled
`python -um pytest` was invisible to the scanner, so the waiter reported "clear
to go" while the lock kept refusing. The `/proc` scan survives only to name a
holder when the lock record is unreadable, and to stand in for the lock entirely
when there is no install (a CI container, a bare checkout).

A pytest that is running but does **not** hold the lock is deliberately not
blocked: it either opted out or belongs to a foreign project.

### When a run is refused

The message names the holder's pid, command and age, plus how to wait:

```bash
python3 scripts/hooks/concurrent_test_guard.py --wait      # default: 2h ceiling
python3 scripts/hooks/concurrent_test_guard.py --wait=600  # or an explicit bound
```

Run it as its **own** command. Chaining the wait and the pytest into one command
is self-defeating: the guard sees the pytest in that same command and blocks the
whole thing, so the wait never starts.

Never hand-roll a waiter. A `pgrep`-based loop matches the waiter's own
`bash -c` argv — the pattern it searches for is right there in its own command
line — so it waits on itself forever, and it matches other sessions' waiters too.

### Levers

Every lever is honoured by **both** layers: if the lock's refusal message tells
you to run something, the hook lets you run it. (It did not, once — the lock
prescribed `GENESIS_PYTEST_LOCK=0` and the hook blocked that exact command, so a
wedged holder meant nobody could test at all. A test now pins the two together.)

| Variable | Effect |
| --- | --- |
| `GENESIS_PYTEST_LOCK=0` | Disable the lock — a deliberate concurrent run. |
| `GENESIS_PYTEST_LOCK_WAIT=1` | Queue until free instead of failing fast. The right shape for a background eval. |
| `GENESIS_PYTEST_LOCK_WAIT_TIMEOUT=<s>` | Bound on that wait (default 7200, capped at 86400). |
| `GENESIS_PYTEST_LOCK_PATH=<file>` | Point the lock at an explicit file instead of the default. |
| `# concurrent-ok` | Trailing-comment override on the Bash command, same idiom as `full-suite-ok`. |

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
