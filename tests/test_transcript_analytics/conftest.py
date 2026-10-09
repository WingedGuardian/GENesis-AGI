import pytest

pytest.importorskip("duckdb")
pytest.importorskip("pyarrow")


@pytest.fixture(autouse=True)
def _isolated_lock(tmp_path, monkeypatch):
    """Never touch the real lock the hourly timer uses."""
    from genesis.transcript_analytics import store

    monkeypatch.setattr(store, "DEFAULT_LOCK", tmp_path / "locks" / "ta.lock")
    from genesis.transcript_analytics import locks

    monkeypatch.setattr(locks, "PUBLICATION_LOCK", tmp_path / "locks" / "publication.lock")
