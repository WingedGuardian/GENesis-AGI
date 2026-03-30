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

## Before Claiming "Tests Pass"

Run the actual command and read the output. Don't guess.
```bash
source ~/genesis/.venv/bin/activate
cd ~/genesis && ruff check . && pytest -v
```
