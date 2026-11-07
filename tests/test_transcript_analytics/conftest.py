import importlib.util

import pytest

# duckdb and pyarrow come from the optional `transcript-analytics` extra. A
# module-level importorskip in a conftest raises Skipped while pytest loads the
# conftest, which aborts the whole collection rather than skipping this
# directory, so leave these modules out of collection instead. The test files
# import pyarrow at module scope, so they could not be collected and skipped.
_EXTRA_MODULES = ("duckdb", "pyarrow")

collect_ignore_glob = (
    [] if all(importlib.util.find_spec(name) for name in _EXTRA_MODULES) else ["test_*.py"]
)


@pytest.fixture(autouse=True)
def _isolated_lock(tmp_path, monkeypatch):
    """Never touch the real lock the hourly timer uses."""
    from genesis.transcript_analytics import store

    monkeypatch.setattr(store, "DEFAULT_LOCK", tmp_path / "locks" / "ta.lock")
