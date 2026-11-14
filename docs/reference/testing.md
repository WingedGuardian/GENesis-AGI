# Testing Guide

Internal shared HTTP qualification uses the exact upgraded-stack interpreter and
explicit files `tests/test_mcp/test_shared_http_admission.py`,
`tests/test_mcp/test_shared_http_policy.py`,
`tests/test_mcp/test_shared_http_runtime.py`, and
`tests/test_observability/test_activity_persistence_scope.py`. Runtime tests own
the real manager lifespan in one task, use actual SDK dispatch/SQLite and
synthetic tools, and cover authentication, profiles/local refusal, activity
suppression, tool/session/body limits, idle expiration and freshness rollback.
Persistence assertions explicitly settle existing asynchronous batches; they
are not an immediate-durability guarantee. These fixtures do not replace owned
loopback progress/cancellation qualification, independent review or landed E2E.

The standalone rollout CLI and captured deploy source are exercised with real
`python -I -S` subprocesses in `tests/test_scripts/test_mcp_rollout_config.py`,
`test_mcp_rollout_failures.py`, and `test_mcp_rollout_repair.py`. Run them with
`tests/test_scripts/test_mcp_rollout.py`, `test_shared_mcp_units.py`, and
`test_deploy_code_only_modes.py` (all under `tests/test_scripts/`). Systemd
calls use fixture shims. Coverage includes missing/incomplete captured source,
future imports and namespace isolation, source replacement, exact launch argv,
UTF-8 under an ASCII locale when available, post-sync CLI arrival, and the
candidate gate's pure classifier. It never executes the candidate operator.
No unused `src/` configuration reader is installed by this change.

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

For the opt-in request-owned database primitive, run
`pytest -q tests/test_db/test_request_database.py tests/test_db/test_request_database_lifecycle.py tests/test_db/test_request_database_tasks.py tests/test_db/test_background_admission.py`.
These use scratch SQLite databases, including independent readers, cancellation,
WAL cursor release and process-death recovery. Four explicit outcome comparisons
keep the actual legacy middleware as the negative control. Shared MCP service E2E
qualification is separate from this primitive coverage.

`tests/test_db/test_request_database_tasks.py` exercises detached ownership,
parent rollback, caller ContextVars, cancellation before start and during work,
shutdown admission, and real activity/heartbeat persistence on scratch SQLite.
It also checks deferred-recall and procedure-refresh bookkeeping when a job
never enters its body. Providers and vector services are not called.

Scoped graph selection and SQL identity parity are covered by
`tests/test_memory/test_graph_request_database.py` and
`tests/test_memory/test_graph_path_identity.py`. They use scratch SQLite with
production table definitions, compare ordered traversal fields against NetworkX,
and check own pending writes and selected-backend fallback. The FalkorDB boundary
is simulated; these tests do not claim a live-engine or shared-service E2E run.

For the service boundary, run the named files
`tests/test_mcp/test_request_database_service.py`,
`tests/test_mcp/test_request_service_initialization.py`,
`tests/test_mcp/test_request_service_tool_bodies.py`, and
`tests/test_mcp/test_request_standalone_health.py`. They use real FastMCP
clients and service initializers with scratch SQLite, testing concurrent calls,
protocol cancellation, finalization errors, durable explicit commits, restart,
and real detached database writers. Standalone health status/alerts assert seeded
SQL results across success, cancellation, enrichment error and shutdown; guard
tests reject unsupported runtime health injection without mutating registration.
Initialization also checks one bootstrap-owned history load, pending-flush
ownership and scoped/legacy transitions. Bootstrap commit/cleanup failures across
all four services preserve registration; bootstrap inside the current task's
active unit is rejected before mutation. External host/provider/vector calls are stubbed;
HTTP/UDS deployment and caller credentials need separate activation checks.

Genesis v3 has **~21,000 test functions** across **~1,345 files** (counted
2026-09-06 by `grep -rEh "^\s*(async )?def test_" tests/ --include=*.py | wc -l`;
parametrized cases make the collected item count higher still). The suite runs
without external service dependencies -- Qdrant, Ollama, and LLM APIs are
mocked in unit tests.

**Runtime.** CI runs the suite FULLY SERIALLY. pytest-xdist is in the `[test]`
extra (pinned in `.github/test-tools/requirements.txt`), but no CI job passes
`-n` yet, and a local `-n` run is unsupported. MEASURED 2026-09-06 over the last 23 successful `main` runs, for
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
- A test that sets its own value with `monkeypatch` still wins.
- `GENESIS_LIVE_E2E=1` (the opt-in live tests) leaves `SECRETS_PATH` and the
  credential variables in place, and the summary says so; the git, gh,
  database, hook-URL and log settings still apply.
- The closure's directory is removed at the end of the run; a child that
  outlives pytest is left with paths that do not exist, so no credentials.
- Not covered:
  - a reader that builds its own path to `secrets.env` or the database
    instead of honouring these variables;
  - a credential under a name the rule does not match: for example `*_OAUTH`,
    `*_BEARER*`, a bare `*_KEY` or `*_PWD`, a DSN, a credential embedded in a
    URL value (`DATABASE_URL`, `HTTP_PROXY`), or a variable pointing at a
    credential file (`AWS_PROFILE`, `DOCKER_CONFIG`, `NETRC`, `KUBECONFIG`);
  - credentials a program finds through the home directory rather than the
    environment (`~/.ssh`, tool config files);
  - a child started with an environment that does not carry these variables,
    including tests that strip every `GIT_*` variable from a child (its git
    then reads the home's user config again);
  - a repository-local `core.askPass` or `credential.helper`.

### The outbound fence: tests never connect to a live service

`tests/_live_outbound.py`, armed after the live fence, refuses a test's
connection to a live service on this machine (any TCP listener; unix sockets
under the real home and the user and dbus run directories), the way CI refuses
it because nothing listens there. A PEP 578 audit hook judges every in-process
`socket.connect`:

- **TCP to this machine** (loopback, `0.0.0.0`/`::`, or one of its own
  addresses): refused when a socket is listening NOW on that port at that
  destination (the same address, or a wildcard on either side) and at least one
  of those sockets does not belong to the pytest process or one of its
  children. Decided at connect time, so a test's own server (in a thread or a
  child) is reachable, a service that started mid-run is still fenced, a test's
  own listener on one address does not vouch for someone else's on another, and
  a port nobody listens on fails on its own.
- **TCP to a private address that is not this machine** (the RFC 1918, CGNAT,
  link-local and ULA ranges in `genesis.util.netclass`): refused on any port.
- Port 53 (DNS) is exempt everywhere.
- **Unix sockets** under the real home, `/run/user/<uid>/` or `/run/dbus/`
  that are listening and not owned by the pytest tree: refused with
  `FileNotFoundError`, which is what CI gives for a socket that does not exist.
  Paths are matched as bytes, so spaces and non-UTF-8 bytes are fine. Without a
  resolvable real home only the two `/run` directories are judged.
- Public addresses are untouched.

A refusal raises `ConnectionRefusedError` (or `FileNotFoundError`) to the
caller, so the code under test takes its CI path. The test is NOT failed; each
refusal is listed in the run summary under "live fence: live connections
refused", and `GENESIS_LIVE_OUTBOUND_REPORT=<file>` writes them per test (an
empty file on a clean run). `connect_ex` raises the refusal instead of
returning `ECONNREFUSED`.

Judging a TCP connect to this machine reads `/proc/net/tcp` and `tcp6`, about
9 ms per connect measured here, even when nothing listens on the port. A test
that polls a local port in a tight loop pays it on every attempt.

A fault inside the judgement never breaks a connect: it is allowed, counted by
exception type, and the summary prints `outbound fence: N judgments failed and
were allowed (...)`.

A test that must reach a live service is marked `live_service` and gated on
`GENESIS_LIVE_E2E=1` (the Qdrant collection, retriever and FalkorDB engine
tests are); the fence lets it connect for its whole run, setup and teardown
included, and the summary lists it. Mark the whole module when its fixtures
connect. A test whose own server daemonizes (double fork, `setsid`) leaves the
pytest tree, so it reads as someone else's: mark it `live_service`.

Known gaps, by design (the OS-level sandbox is the boundary): connections made
by child processes, connects done in C (curl_cffi), UDP and unix datagram
sockets; unix sockets outside the three directories above (`/run/systemd`, a
VPN daemon's socket); a thread an earlier unmarked test left running, while a
`live_service` test runs; a unix socket path
containing a newline (it cannot be represented in `/proc/net/unix`); and an
IPv6 link-local scope id, which `/proc/net/tcp6` does not record, so two
listeners with the same link-local address on different interfaces are judged
as one.

## Shared MCP qualification environments

Ordinary CI keeps its existing runtime dependency ranges. The shared-only
protocol and shared CLI suites require the exact versions admitted by
`genesis.mcp.shared_http._settings.require_qualified_stack`; an unsupported
ordinary environment explicitly skips the eleven SDK-specific modules before
SDK imports. The caller-path policy, read-location API and ordinary preparation
tests remain ordinary tests; policy and preparation also run in the qualified
protocol selection.
The health selection snapshot tests in `test_local_health_selection.py` remain
ordinary tests too. They verify the real P7 program, exclusions, current limits,
module cleanup and a configuration change during readiness without contacting a
manager. The pinned job also runs them and the existing service-settings tests.
Pure configuration and missing/later-version refusal tests remain ordinary
tests in `tests/test_mcp/test_shared_http_settings.py`.

`.github/workflows/shared-mcp-qualification.yml` installs the separate
`.github/shared-mcp/requirements.txt` constraints alongside the existing test-tool
pins. It runs for relevant PRs across base branches, including stacked PRs, with
read-only permissions. `GENESIS_REQUIRE_SHARED_MCP_QUALIFICATION=1` is test-only:
qualification refusal becomes a collection error in this job. No production
version, feature, credential or rollout gate changes.

The protocol step names admission, policy, actual HTTP, shared CLI, explicit
forwarder and forwarder HTTP-error modules, the caller-path suites, ordinary
health/memory preparation, and SDK-independent credential reader tests. The CLI checks start all four real factories with disposable local
dependencies, exercise catalog/authentication/read calls over TCP, and observe
SDK shutdown and registered-owner drainage before resource closure on SIGTERM.
They also exercise path/abstract readiness datagrams after bind and cleanup on
failed notification, bind failure and shutdown requested during startup. The
memory notification-failure case checks its actual provider/cache closers.
Forwarder tests run real stdio pipes against those factories and a disposable
P4 service: full catalogs/seeds, caller separation/auth refusal, raw admission,
typed IDs, cancellation, EOF under initialization/output pressure, long/large
replies, legacy callbacks, busy neighbors, malformed JSON/SSE and no replay
after a recorded effect. HTTP-error tests enumerate redirect/status/content-type
and typed-business-error branches through the actual SDK with in-memory HTTP.
Caller-path sessions compare actual ingestion/document/resume/golden-set/replay/
task wrappers against ordinary caller-cwd oracles, keeping file loaders, plan
guards, owned SQLite and original manifest identities. External execution uses
explicit synthetic provider, CC and PageIndex boundaries. They exercise both
protocol eras, colliding server files, old cache units, bad/misbound metadata
and valid unhinted absolute inputs. Both HTTP routes retain auth/authority,
envelope/body, catalog and shared session-cap/cleanup tests. A missing-route
fixture models an older backend's route table at startup and after rollback;
this does not certify an installed older release. Actual wrappers additionally
refuse lost scope before orchestrator/runtime construction. Ordinary API tests
cover cache states, copied originals, batch spelling/symlinks, tree keys/HOME
guards and real PDF extraction with pool shutdown.
Ordinary preparation checks enter the actual health/memory lifespans through
the public in-memory transport, compare complete catalogs, preserve integer and
string handler IDs, and read seeded SQLite data. They cover both protocol eras,
external/validator discovery and direct-call refusal, degraded/missing DB and
memory read-pool success/fallback/closure. The ordinary launchers remain runtime
consumers of these preparations and retain their separate stdio regression suite.
For local pre-change comparison, `GENESIS_ORDINARY_BOOTSTRAP_ORACLE` selects a
retained launcher script only inside the disposable capsule; the candidate copy
is restored after each completed child. Without it, CI compares the current
bootstrap and direct prepared consumer against the independent names/data checks.
`GENESIS_ORDINARY_PREPARATION_EVIDENCE_DIR` optionally preserves synthetic child
receipts outside pytest's successful-temp-directory cleanup.
The forwarder module also tests the actual ordinary health argv from a neutral
cwd against a separately owned shared health service. Distinct seeded local and
remote status values and body receipts establish where calls executed. The
ordinary/mixed pair compares complete SDK catalogs and progress/native IDs;
initialization metadata, mixed protocol eras, catalog refusal/retry and active
local cancellation/EOF cover their respective boundaries. Browser implementation
and external provider/process boundaries are explicitly synthetic/fenced; these
are not native client, browser or payment turns. The protocol-only matrix uses a
synthetic remote leg and the actual local peer against an ordinary SDK oracle.
`GENESIS_LOCAL_HEALTH_EVIDENCE_DIR` optionally retains synthetic wire/body/close
receipts outside successful-temp cleanup. Uvicorn re-raises captured SIGTERM
after graceful shutdown in this qualified version, so the backend check requires
the expected signal exit together with completed SDK/drain/resource events.
They neither contact a native client/provider nor activate routing.
Its fresh JUnit report must pass `scripts/check_skip_ceiling.py` with zero skips
and a floor of 509 cases. Installation, collection, test or report-check failure
fails the job; missing or filtered workflow runs do not qualify a PR. Closing
checks its actual head/base result. Ordinary skip-budget changes require observed
skip identities; this workflow does not raise that budget automatically.

For a local run, use an already qualified isolated interpreter and the same
strict test flag, then run the workflow's report check. Changing installed
packages or enabling shared services is not part of that test command.

`test_shared_notify.py` exercises owned local datagrams and error propagation
without SDK qualification. `test_shared_mcp_templates.py` reuses both actual
renderer loops and executes their activation sections with nondelegating command
shims, checking literal paths, stamps and keep/take behavior without starting a
unit. Successful per-test payload/event files require both an explicit scratch
`--basetemp` and `-o tmp_path_retention_policy=all`; the normal policy removes
successful test directories. Read-only `systemd-analyze verify` can validate
rendered syntax with generators disabled; it does not execute or qualify a live
service.

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

## Request-owned resource coverage

Shutdown grace and actual synchronous completion are covered by
`tests/test_db/test_request_database_drain.py`,
`tests/test_db/test_request_database_drain_states.py`,
`tests/test_db/test_physical_work.py`, and
`tests/test_db/test_physical_work_states.py`. These cover real owned-file use,
queued/running executor cancellation, ambiguous submission, joined children,
context propagation and legacy cancellation. The connection budget is tested
separately from the number of physical operations. Fault fixtures release and
join their real workers; an import failure or unrelated timeout is not evidence
of forbidden resource use. Production factories and shared activation remain
separate qualifications.

`tests/test_memory/test_resource_thread_ownership.py` exercises all nine migrated
memory boundaries with gated real file I/O, cancellation and a real scratch
database lease. `tests/test_memory/test_tag_index_ownership.py` covers independent
background admission, physical scan drain, refusal/creation failure, prestart
cancellation, retriever propagation and the body-completed/done-callback gap.
`tests/test_memory/test_tag_index_swr.py` retains legacy stale-query behavior.

Owned shared bootstrap coverage lives in `tests/test_mcp/test_shared_service_settings.py`,
`test_shared_resource_factories.py`, `test_shared_launch_selection.py` and
`test_shared_runner.py`, plus `tests/test_memory/test_embedding_resource_lifecycle.py`.
Factory argument tests use real canonical SQLite/facades with captured initializer
arguments and provider doubles; they are not real-provider or native-client E2E.
`test_shared_runner_lifecycle.py` runs disposable child processes with real Uvicorn
and inert ASGI/resource dependencies to verify signal replay, cancelled startup,
bind errors, failed lifespan and cancelled/forced shutdown ordering. These tests
do not send signals to another session or start a deployed service.

### Pending plan-bookmark consumption

`tests/test_mcp/test_plan_bookmark_consumption.py` exercises the actual helper
against disposable HOME and GENESIS_HOME directories: explicit versus default
input, absent/malformed/read/create failures, lock reuse/busy/open failure,
separate OS readers, killed holders, session-prefix decoys and label exclusion.
Owned RequestDatabase jobs cover cancellation before entry and while waiting
for a background unit; a held manager proves no lock spans its await.
`plan_bookmark_consumer_child.py` is the minimal owned process observer.
The existing real SDK tool-body/SQLite-index test additionally checks a short
caller hint against a conflicting server directory. Provider/vector/extraction
boundaries in that test remain synthetic; it is not full mixed stdio/TCP memory
qualification. The native environment-label path requires integrated-main
evidence, separately from direct request-context guard tests.

`test_plan_bookmark_context.py` tests lexical validation, immutable metadata
preparation, caller-identity provenance, overlap and cancellation resets.
`test_plan_bookmark_http.py` exercises authenticated SDK calls into the real
memory guard and bookmark SQLite index, caller/server decoys, rejected arguments,
static credentials, labelled forwarders and distinct overlapping inputs.
`test_plan_bookmark_peer.py` runs the actual RemotePeer dispatcher over an owned
TCP service for both protocol eras, reserved-key refusal, default opt-out, and
missing/wrong capability on initial and explicitly recreated generations.
It checks opted peers use `/mcp/v2` for both protocol eras, legacy GET/DELETE,
default v1 opt-out, and cached-generation refusal when the execution route is
removed. HTTP cases retain raw envelope, auth, authority and body limits on v2;
actual ingestion E2E also checks caller-file identity through that route.
Backend process replacement needs a separate real-process control/counterexample,
not only explicit `RemotePeer.close()`. Providers,
vectors and extraction remain synthetic. This qualifies the explicit contract,
not the still-unwired ordinary memory frontend or native environment-label path.
