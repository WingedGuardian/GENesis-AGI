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

Genesis v3 has **2505+ tests** across all subsystems. The suite runs without
external service dependencies -- Qdrant, Ollama, and LLM APIs are mocked in
unit tests.

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
