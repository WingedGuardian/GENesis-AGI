# Testing Guide

## Running Tests

All tests run from the genesis project root using pytest:

```bash
source ~/genesis/.venv/bin/activate
cd genesis

# Full suite
pytest -v

# Specific subsystem
pytest -v tests/test_memory/
pytest -v tests/test_resilience/

# Linting (run before committing)
ruff check .

# Both (standard pre-commit check)
ruff check . && pytest -v
```

## Test Suite Overview

Genesis v3 has **~21,000 test functions** across **~1,345 files** (counted
2026-09-06 by `grep -rEh "^\s*(async )?def test_" tests/ --include=*.py | wc -l`;
parametrized cases make the collected item count higher still). The suite runs
without external service dependencies -- Qdrant, Ollama, and LLM APIs are
mocked in unit tests.

**Runtime.** CI runs the suite FULLY SERIALLY (no pytest-xdist; it is not a
dependency). MEASURED 2026-09-06 over the last 23 successful `main` runs, for
the **`test` job specifically** (`gh api .../actions/runs/<id>/jobs`, name ==
`test`): min 19m38s, median 29m22s, max 31m15s. Do not quote workflow-RUN
duration here — it is ~20 minutes longer because it spans 12 sibling jobs plus
queue time, and it is not what any of the ceilings below bound.

Two ceilings bound the suite, and they are different things:

- `timeout = 600` in `pyproject.toml` is pytest-timeout's **per-test** limit —
  one hung test is killed and the suite continues. It is NOT a suite budget.
  It applies wherever `pytest-timeout` is installed: always in CI, and on an
  install once bootstrap's non-fatal step has installed the `[test]` extra
  (`install_runtime_extras`; a code-only deploy warns when it is missing).
  Without it a run starts with a warning saying so, because a hung test then
  holds the box-wide test lock until it is killed by hand.
- `timeout-minutes: 180` on CI's `test` job bounds the whole job, for a hang
  the per-test timeout cannot catch. It must stay above (max job + one
  per-test kill) — 31m15s + 10m ≈ 41m — or it would pre-empt the per-test
  limit and make it dead code.

The per-test cost of individual tests had never been measured; CI now prints
`--durations=25` so the slowest are visible in every run's log. Read them
before changing either ceiling.

## Test Directory Structure

Tests are organized by subsystem, mirroring `src/genesis/`:

```
tests/
  conftest.py                  # Shared fixtures (DB, mock providers, etc.)
  test_autonomy/               # Earned autonomy, verification gates
  test_awareness/              # Awareness loop, signal monitoring
  test_bookmark/               # Session bookmarks, temporal awareness
  test_browser/                # Browser automation
  test_calibration/            # Confidence calibration
  test_cc/                     # Claude Code session management
  test_channels/               # Telegram, Discord channel adapters
  test_content/                # Content generation, voice
  test_dashboard/              # Neural monitor dashboard
  test_db/                     # Database layer, migrations
  test_health_data.py          # Health data collection
  test_health_mcp.py           # Health MCP server
  test_hooks/                  # PreToolUse hooks, behavioral linter
  test_inbox/                  # Inbox monitoring
  test_learning/               # Self-learning loop, lessons
  test_mcp/                    # MCP server integration
  test_memory/                 # Episodic memory, knowledge base, embeddings
  test_modules/                # Core module loading
  test_observability/          # Event logging, heartbeats, cost tracking
  test_observation_consumption.py
  test_outreach/               # Proactive outreach
  test_perception/             # Signal perception, salience
  test_pipeline/               # Processing pipeline
  test_providers/              # LLM provider adapters
  test_qdrant/                 # Qdrant vector DB operations
  test_recon/                  # Reconnaissance system
  test_reflection/             # Reflection engine (micro/light/deep/strategic)
  test_research/               # Research capabilities
  test_resilience/             # Circuit breakers, composite state, fallback
  test_routing/                # Model routing, failover
  test_runtime/                # Runtime bootstrap, lifecycle
  test_scripts/                # Utility scripts
  test_security/               # Security checks, delete guard
  test_smoke.py                # Quick smoke test
  test_surplus/                # Cognitive surplus, compute availability
  test_tts_comprehensive.py    # Text-to-speech
  test_tts_config.py           # TTS configuration
  test_ui/                     # UI overlay, dashboard
  test_util/                   # Utility functions
  test_voice_delivery.py       # Voice delivery pipeline
  test_web/                    # Web tools
  integration/                 # Integration tests (require running services)
```

## Key Testing Patterns

### Subsystem Isolation

Each test directory is self-contained. Tests mock external dependencies
(Qdrant client, LLM API calls, filesystem) so they run without live services.
The shared `conftest.py` at the test root provides common fixtures for
database setup, mock providers, and temporary directories.

### Mock Patterns

- **LLM calls**: Mocked via provider adapters. Tests assert on prompt
  construction and response handling, not on actual LLM output.
- **Qdrant**: `AsyncQdrantClient` is mocked. Collection operations return
  controlled fixtures.
- **Database**: Tests use temporary SQLite databases created per-test or
  per-module. The `genesis_db` fixture in `conftest.py` handles setup and
  teardown.
- **Asyncio**: Tests use `pytest-asyncio` for async test functions. Background
  tasks use `tracked_task()` which is tested for proper lifecycle management.

### Fixture Organization

Shared fixtures live in `tests/conftest.py`. Subsystem-specific fixtures live
in `tests/test_<subsystem>/conftest.py`. This layering keeps fixtures close
to where they are used while avoiding duplication.

### Safety-Critical Tests

Some tests verify safety invariants that protect production data and system
stability:

- **Qdrant delete guard** (`test_security/`): Verifies that bulk collection
  deletes are blocked unless explicitly overridden.
- **PGID validation** (`test_cc/`): Verifies that process kill operations
  validate PGID > 1 before calling `os.killpg()`.
- **Hook enforcement** (`test_hooks/`): Verifies that PreToolUse hooks
  correctly block dangerous tool invocations.

### The live fence: tests never run real commands on the machine

`tests/_live_fence.py`, armed for every run by `tests/conftest.py`, stops a
test from starting a real process against the machine it runs on. It is a
guardrail (code in the test process could get around it), not a sandbox.

- **Refused, and the test fails:**
  - `claude` (anything but a bare `--version` or `--help`; `claude.exe` and
    `claude-code` count) and `sudo`;
  - any `systemctl`/`systemd-run`/`gh`/`loginctl` called by a real path outside
    the test's temp dirs (`tempfile.gettempdir()` and pytest's basetemp);
  - a deploy script run against live state — refused by the SCRIPT, not the
    fence: the fence exports `GENESIS_TEST_FENCE_LIVE_ROOTS` (this checkout's
    real root, plus the real home's `~/.genesis`/`~/genesis` when they exist,
    plus a root named in `GENESIS_LIVE_FENCE_LIVE_ROOT`) and
    `GENESIS_TEST_FENCE_LOG` (the fence's own log), and
    `scripts/lib/live_system_guard.sh`'s `refuse_if_test_fenced_live` makes each
    deploy script (`scripts/{update,deploy_code_only,restore,backup,
    bootstrap}.sh` and `deploy_candidates`) print `REFUSED (test fence): ...`,
    append the same line to the log, and exit 97 when its acting root,
    `HOME`, `GENESIS_HOME` or `HOME/.genesis` is at or under a listed root
    (or a path it must check cannot be resolved provably). This runs inside the script after bash's own expansion, so no
    launch spelling escapes it — `bash script`, `bash -c`, `BASH_ENV`,
    `--rcfile`, a here-string, whatever a child starts.

  The in-process refusal is a `BaseException`, so an `except Exception`
  fallback in the code under test cannot catch it and try another route; in a
  forked child the child exits 127 instead.
- **Answered like CI, and reported:** `systemctl`, `systemd-run`, `gh` and
  `loginctl` called by bare name (and `claude --version`/`--help`) reach a stub
  that answers as the real tool does on a CI runner, where nothing is reachable
  ("Failed to connect to bus", exit 1; `gh` unauthenticated, exit 4). The test
  is not failed; the call is listed under "live-state reads answered by a
  stub". Stub the call in the test if its result matters. A stub exists only
  for a tool installed on the machine, so `shutil.which` still reports a missing
  tool as missing.
- **A refusal drained after its test ended** (a detached child) fails the
  whole session, listed under "refusals outside their test". Reporting ends at
  the final session drain. The unique shim directory remains for surviving
  children until normal temp cleanup, so inherited PATH lookups keep refusing.
- Only the resolved program is ever judged in-process — never argv or a shell
  string. Any program inside the test's temp dirs runs, so a test that needs a
  fake of one of these tools puts it in `tmp_path`.
- Give a test that runs a deploy script a private `HOME` and point the script's
  root at a fixture tree (the `tests/test_scripts/_deploy_station.py` fixture
  is the model); the script then runs its normal path.
- Two explicit opt-outs, each listed in the run summary:
  - `user_manager`: may run the real `systemd-run` (a transient `--user` scope, a
    unit check) and gets the real session-bus variables back.
  - `real_tool("gh", ...)`: deliberately runs the named installed tools (a
    table checked against their own `--help`, a kernel probe).
  Deploy scripts against live state stay refused for both.
- Known gaps, by design (an in-process fence sees only this interpreter's own
  launches; the OS-level sandbox is the answer):
  - a program a child process starts by absolute path;
  - a child given a hand-built `env` without the fence's `PATH` and bus
    variables: a script that defaults `XDG_RUNTIME_DIR` when it is unset then
    reaches the real user manager;
  - whatever a child shell string or a real `systemd-run` in a `user_manager`
    test goes on to start;
  - the stubs answer like CI only approximately: `claude --version` exits 127
    where CI raises "not found", and a system-level `systemctl` also gets "Failed
    to connect to bus".

### The live closure: tests run without this machine's credentials or live state

`tests/_live_closure.py`, armed right after the live fence, takes credentials,
live state paths and endpoints out of the process environment before any test
runs, so a test behaves as it does on CI, where none exist. Child processes
inherit it.

- `SECRETS_PATH` names a file that does not exist, so every `secrets.env`
  loader that honours it loads nothing. Each test gets its own such path under
  its `tmp_path`, so a script that writes there (`restore.sh`) cannot leave a
  file for the next test. A test that runs `backup.sh` or `restore.sh` against
  a sandbox `GENESIS_DIR` drops `SECRETS_PATH` from the child's environment, so
  the sandbox decides where the secrets file is, as on CI.
- Credential-shaped variables are removed for the run: `SSH_AUTH_SOCK`,
  `GH_TOKEN`, `GITHUB_TOKEN`, `GIT_ASKPASS`, `SSH_ASKPASS`, a bare `API_KEY`,
  names starting `VSCODE_GIT_`, and names containing `_API_KEY`, `_SECRET`,
  `PASSWORD`, `PASSPHRASE`, `WEBHOOK`, `CREDENTIAL`, `_ACCESS_KEY` or
  `_PRIVATE_KEY`, starting `API_KEY_`, or ending `_TOKEN`, `_PASS`, `_KEY_ID`
  or `_AUTH_CONFIG`. The run summary lists their names, never values.
- `GH_CONFIG_DIR` points at an empty directory, `GIT_TERMINAL_PROMPT=0`,
  `GIT_SSH_COMMAND=false`, and `GIT_CONFIG_GLOBAL=/dev/null`,
  `GIT_CONFIG_NOSYSTEM=1` and `GIT_CONFIG_SYSTEM=/dev/null` close git's user and
  system configuration (and any credential helper in it) whatever `HOME` or
  `XDG_CONFIG_HOME` say, even for an explicit `git config --system`. Config git
  takes from the environment (`GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_COUNT` and
  its `KEY_n`/`VALUE_n`) is removed. A test that needs a git identity sets it
  itself, as on CI.
- `GENESIS_DB_PATH` is set when the plugin is imported, before pytest imports
  any `tests/test*/conftest.py`, so a module that resolves the database path at
  import (or as a default argument) binds the closure's file, never the
  checkout's `data/genesis.db`. Each test then gets its own file
  (`_isolate_genesis_db_path` in `tests/conftest.py`), the same one the
  in-process patch returns. If something changes the variable between import
  and configure, the run says so on stderr and restores it.
- `GENESIS_PROACTIVE_HOOK_URL` points at a closed port, so a test running the
  memory hook as a child never posts to the live server.
- `GENESIS_MCP_LOG_DIR` names a directory that does not exist, so importing the
  health MCP opens no log in the shared `~/tmp`.
- `HOME` names a private directory for the whole session, created when the
  plugin is imported (before any `genesis` module loads, so nothing binds the
  real home first), with `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME`
  and `XDG_STATE_HOME` inside it. Anything resolved through `Path.home()`, `~`
  or those variables (tokens, the Guardian's shared policy file, caches,
  stamps, logs, `~/tmp` scratch, `~/.claude`, `~/.genesis`, `~/.ssh`,
  `~/.aws`) lands there instead. It starts with only `tmp/`. It sits in the
  real home's big-tmp directory (`GENESIS_BIG_TMP`, else `~/tmp`) where the
  basetemp redirect applies, else in the default temp dir, and is removed at
  the end of the run; a killed run's home ages out through the daily `~/tmp`
  prune. The box lock and basetemp are placed with `HOME` set back to what it
  was when the run started, so both keep their locations. `tests._live_fence.real_home()` names
  the real home for a test that must use it, and `PLAYWRIGHT_BROWSERS_PATH`
  keeps pointing at the real browser cache so the UI tests find their browser.
  The run summary names the home, and any `genesis` module that was already
  loaded when it was set (none is expected).
- A test that sets its own value with `monkeypatch` still wins.
- `GENESIS_LIVE_E2E=1` (the opt-in live tests) leaves `SECRETS_PATH`, the
  credential variables and `HOME` in place, and the summary says so; the git,
  gh, database, hook-URL and log settings still apply.
- The closure's directory is removed at the end of the run; a child that
  outlives pytest is left with paths that do not exist, so no credentials.
- Not covered:
  - a reader that builds its own path to `secrets.env` or the database
    instead of honouring these variables;
  - a credential under a name the rule does not match: for example `*_OAUTH`,
    `*_BEARER*`, a bare `*_KEY` or `*_PWD`, a DSN, a credential embedded in a
    URL value (`DATABASE_URL`, `HTTP_PROXY`), or a variable pointing at a
    credential file (`AWS_PROFILE`, `DOCKER_CONFIG`, `NETRC`, `KUBECONFIG`);
  - a path resolved from the password database rather than `HOME` (OpenSSH
    finds `~/.ssh` that way), and a child started with an environment that has
    no `HOME` at all (Python and bash then fall back to the real home);
  - an explicit `GENESIS_HOME` a test or the environment sets, and an explicit
    `GENESIS_BIG_TMP`, which `big_tmp_dir()` keeps using as given;
  - other variables that already hold a path under the real home when the
    run starts (a plugin's data directory, a session socket): they are
    passed through unchanged;
  - `tempfile` output, which goes to `TMPDIR` (on some installs a directory in
    the real home);
  - a child started with an environment that does not carry these variables
    (a test that strips every `GIT_*` variable from a child still leaves it
    the private `HOME`, so its git reads only the empty test home's config);
  - a repository-local `core.askPass` or `credential.helper`.

## Integration Tests

Integration tests in `tests/integration/` require running external services
(Qdrant, Ollama). They are not run as part of the standard `pytest` invocation.
Run them explicitly when you have the services available:

```bash
pytest -v tests/integration/
```

## Adding Tests

When adding a new subsystem or feature:

1. Create `tests/test_<subsystem>/` with an `__init__.py`.
2. Add subsystem-specific fixtures in `tests/test_<subsystem>/conftest.py`.
3. Mock external dependencies; do not require live services for unit tests.
4. Verify the new tests pass in isolation: `pytest -v tests/test_<subsystem>/`.
