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
  It applies wherever `pytest-timeout` is installed: always in CI, and locally
  only if you installed it (it ships in the `[test]` extra, which no install
  path installs). Without it a run starts with a warning saying so, because a
  hung test then holds the box-wide test lock until it is killed by hand.
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
  - a deploy script (`scripts/{update,deploy_code_only,restore,backup,
    bootstrap}.sh`) run with the real `HOME`, or acting on the deployed checkout
    (`~/genesis`, plus a checkout named in `GENESIS_LIVE_FENCE_LIVE_ROOT`, which
    counts only if it holds `scripts/update.sh`). Where each script acts:
    `update.sh` and `deploy_code_only.sh` on their root override
    (`GENESIS_UPDATE_ORIG_DIR`, `GENESIS_DEPLOY_ROOT`) or else the checkout they
    live in; `backup.sh` and `restore.sh` on `GENESIS_DIR` or else
    `$HOME/genesis`; `bootstrap.sh` on the checkout it lives in. A deploy script
    in a `scripts/` directory under a temp dir is still refused with the real
  `HOME`. A command named like
    one whose file cannot be found (`cd <dir> && ./update.sh`) is judged by the
    `HOME` rule alone.

  Every command is judged as far as the repo's own parser can read it:
  wrappers (`env`, `timeout`, `sudo`, ...) are judged and stripped, `sh -c`
  strings and here-strings fed to a shell are opened, every `PATH=` the line
  sets is honored, and heredoc bodies that only feed data to a command
  (`cat <<EOF`) are skipped. The refusal is a
  `BaseException`, so an `except Exception` fallback in the code under test
  cannot catch it and try another route; in a forked child the child exits 127
  instead.
- **Answered like CI, and reported:** `systemctl`, `systemd-run`, `gh` and
  `loginctl` called by bare name (and `claude --version`/`--help`) reach a stub
  that answers as the real tool does on a CI runner, where nothing is reachable
  ("Failed to connect to bus", exit 1; `gh` unauthenticated, exit 4). The test
  is not failed; the call is listed under "live-state reads answered by a
  stub". Stub the call in the test if its result matters. A stub exists only
  for a tool installed on the machine, so `shutil.which` still reports a missing
  tool as missing.
- **A refusal that lands after its test ended** (a detached child) fails the
  whole session, listed under "refusals outside their test".
- Any program inside the test's temp dirs that is not a deploy script runs, so
  a test that needs a fake of one of these tools puts it in `tmp_path`.
- Give a test that runs a deploy script a private `HOME` and point the script's
  root at a fixture tree (the `tests/test_scripts/_deploy_station.py` fixture
  is the model).
- Two explicit opt-outs, each listed in the run summary:
  - `user_manager`: may run the real `systemd-run` (a transient `--user` scope, a
    unit check) and gets the real session-bus variables back; the command that
    `systemd-run` would start is still judged, against the user manager's PATH.
  - `real_tool("gh", ...)`: deliberately runs the named installed tools (a
    table checked against their own `--help`, a kernel probe).
  Deploy scripts against live state stay refused for both.
- Known gaps, by design (an in-process fence sees only this interpreter's own
  launches and what its parser can read; the OS-level sandbox is the answer):
  - a program a child process starts by absolute path;
  - a child given a hand-built `env` without the fence's `PATH` and bus
    variables: a script that defaults `XDG_RUNTIME_DIR` when it is unset then
    reaches the real user manager;
  - what the parser does not open (`eval`, a command held in a variable, a
    function body, a script piped or redirected into a shell, `find -exec`,
    `su`/`runuser`/`flock`/`script`/`unshare`/`setpriv`): only the PATH stubs
    judge these, so a bare name is caught and an absolute path is not;
  - a `systemd-run` started by a child in a `user_manager` test;
  - the stubs answer like CI only approximately: `claude --version` exits 127
    where CI raises "not found", and a system-level `systemctl` also gets "Failed
    to connect to bus".

### The live closure: tests run without this machine's credentials

`tests/_live_closure.py`, armed right after the live fence, takes credentials
and live state paths out of the process environment before any test runs, so
a test behaves as it does on CI, where none exist. Child processes inherit it.

- `SECRETS_PATH` names a file that does not exist, so every `secrets.env`
  loader that honours it loads nothing. Each test gets its own such path under
  its `tmp_path`, so a script that writes there (`restore.sh`) cannot leave a
  file for the next test. A test that runs `backup.sh` or `restore.sh` against
  a sandbox `GENESIS_DIR` drops `SECRETS_PATH` from the child's environment, so
  the sandbox decides where the secrets file is, as on CI.
- Credential-shaped variables are removed for the run: `SSH_AUTH_SOCK`,
  `GH_TOKEN`, `GITHUB_TOKEN`, and names containing `_API_KEY`, `_SECRET`,
  `PASSWORD`, `PASSPHRASE`, `WEBHOOK`, `CREDENTIAL`, `_ACCESS_KEY` or
  `_PRIVATE_KEY`, starting `API_KEY_`, or ending `_TOKEN`, `_PASS` or
  `_KEY_ID`. The run summary lists their names, never values.
- `GH_CONFIG_DIR` points at an empty directory, `GIT_TERMINAL_PROMPT=0`, and
  `GIT_SSH_COMMAND=false`.
- `GENESIS_PROACTIVE_HOOK_URL` points at a closed port, so a test running the
  memory hook as a child never posts to the live server.
- `GENESIS_DB_PATH` is set per test (`_isolate_genesis_db_path` in
  `tests/conftest.py`) to the same temp database the in-process patch returns.
- A test that sets its own value with `monkeypatch` still wins.
- `GENESIS_LIVE_E2E=1` (the opt-in live tests) leaves secrets in place, and the
  summary says so; the git, gh and hook-URL settings still apply.
- Not covered:
  - a reader that builds its own path to `secrets.env` instead of honouring
    `SECRETS_PATH`;
  - credentials kept in other files (`~/.claude`, `~/.genesis`, `~/.ssh`,
    `~/.git-credentials`);
  - state paths other than the database: `GENESIS_HOME` is not redirected,
    and a reader of the fixed `~/genesis/data/genesis.db` path ignores
    `GENESIS_DB_PATH`.

### The outbound fence: tests never connect to a live service

`tests/_live_outbound.py`, armed after the live closure, refuses a test's
connection to a service running on this machine, the way CI refuses it because
nothing listens there. A PEP 578 audit hook judges every in-process
`socket.connect`:

- **TCP to this machine** (loopback, `0.0.0.0`/`::`, or one of its own
  addresses): refused when the port is listening NOW and no listening socket on
  it belongs to the pytest process or one of its children. Decided at connect
  time, so a test's own server (in a thread or a child) is always reachable, a
  service that started mid-run is still fenced, and a port nobody listens on
  fails on its own.
- **TCP to a private address that is not this machine** (the RFC 1918, CGNAT,
  link-local and ULA ranges in `genesis.util.netclass`): refused on any port.
- Port 53 (DNS) is exempt everywhere.
- **Unix sockets** under the real home, `/run/user/<uid>/` or `/run/dbus/`
  that are listening and not owned by the pytest tree: refused with
  `FileNotFoundError`, which is what CI gives for a socket that does not exist.
- Public addresses are untouched.

A refusal raises `ConnectionRefusedError` (or `FileNotFoundError`) to the
caller, so the code under test takes its CI path. The test is NOT failed; each
refusal is listed in the run summary under "live fence: live connections
refused", and `GENESIS_LIVE_OUTBOUND_REPORT=<file>` writes them per test (an
empty file on a clean run).

A test that must reach a live service is marked `live_service` and gated on
`GENESIS_LIVE_E2E=1` (the Qdrant collection, retriever and FalkorDB engine
tests are); the fence lets it connect for its whole run, setup and teardown
included, and the summary lists it. Mark the whole module when its fixtures
connect.

Known gaps, by design (the OS-level sandbox is the boundary): connections made
by child processes (the memory hook's URL is redirected by the live closure),
connects done in C (curl_cffi), and UDP.

### The write audit: writes into the real home are reported

`tests/_live_writes.py`, armed last, reports (never fails) the writes a test
makes into the real home outside its temp dirs, for the operations it hooks: `open` with write intent,
`os.remove`/`rename`/`replace`/`mkdir`/`rmdir`/`truncate`/`symlink`/`link`,
`shutil.rmtree`, and an SQLite connect to a file that is not opened read-only.
It also reports reads of the real credential files (the deployed `secrets.env`,
`~/.claude/.credentials.json`, `~/.config/gh/hosts.yml`,
`~/.genesis/cc_oauth_token.env`).

- Allowed, so never reported: `tempfile.gettempdir()`, pytest's basetemp, the
  box lock file, `/dev`, and `__pycache__` / `.pytest_cache` directories.
- Each write is classed by where it lands (the deployed checkout, this checkout,
  `~/.genesis`, `~/.claude`, elsewhere in the home) and attributed to the test.
- The run summary counts tests per class under "writes outside the test's temp
  dirs"; `GENESIS_LIVE_WRITES_REPORT=<file>` writes one tab-separated line per
  (test, class, operation, path), on every run.
- A hit is a write that happens: an attempt the kernel refuses or that changes
  nothing (`mkdir(exist_ok=True)` on an existing directory, `unlink` of a missing
  file, reopening an existing file without writing) is not recorded. A relative
  `os.open` is judged against the cwd and labelled so (its audit event carries no
  `dir_fd`). Writes during collection count as "outside any test".
- Not covered: writes made by child processes, writes without an audit event,
  metadata changes (`chmod`, `utime`, `setxattr`, `touch` of an existing file),
  `mkfifo`/`mknod`, SQLite writes through an already-open connection, and
  `file://localhost` or percent-encoded SQLite URIs. The OS-level sandbox is the
  boundary there.

The list it produces is the to-do list for moving each such test under its own
`tmp_path`; once it is empty, the audit can refuse instead of report.

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
