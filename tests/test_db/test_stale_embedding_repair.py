"""Tests for the stale procedure-embedding repair (d0011).

Distinct from test_embedding_backfill.py (the promoter's NULL backfill): here the
stored embedding is PRESENT but describes an OLDER principle than the row's
current text — the pre-#1277 refine residue. Uses a deterministic fake embedder
(text -> reproducible 1024-vec) so two distinct texts map to near-orthogonal
vectors (cosine ~0) and the same text to an identical vector (cosine 1.0),
cleanly straddling FRESH_MATCH_THRESHOLD. No network.
"""

from __future__ import annotations

import hashlib
import random
import sqlite3

import pytest

from genesis.db.data_migrations import stale_embedding_repair as sr
from genesis.learning.procedural.embedding import pack_embedding


def _fake_vec(text: str) -> list[float]:
    seed = int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")
    rng = random.Random(seed)
    return [rng.uniform(-1.0, 1.0) for _ in range(1024)]


class _FakeProvider:
    def __init__(self):
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        return _fake_vec(text)


def _make_db(tmp_path) -> str:
    path = str(tmp_path / "t.db")
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE procedural_memory ("
        " id TEXT PRIMARY KEY, task_type TEXT, principle TEXT,"
        " principle_embedding BLOB, version INTEGER DEFAULT 1,"
        " deprecated INTEGER DEFAULT 0, quarantined INTEGER DEFAULT 0,"
        " created_at TEXT)"
    )
    con.commit()
    con.close()
    return path


def _insert(path, id_, principle, version, emb_text, deprecated=0, quarantined=0):
    blob = pack_embedding(_fake_vec(emb_text)) if emb_text is not None else None
    con = sqlite3.connect(path)
    con.execute(
        "INSERT INTO procedural_memory "
        "(id, task_type, principle, principle_embedding, version, deprecated, "
        " quarantined, created_at) VALUES (?,?,?,?,?,?,?,?)",
        (
            id_,
            "code_review",
            principle,
            blob,
            version,
            deprecated,
            quarantined,
            "2026-07-10T00:00:00Z",
        ),
    )
    con.commit()
    con.close()


def _emb_of(path, id_):
    con = sqlite3.connect(path)
    row = con.execute(
        "SELECT principle_embedding FROM procedural_memory WHERE id = ?", (id_,)
    ).fetchone()
    con.close()
    return row[0]


def test_heals_stale_leaves_fresh_and_v1_untouched(tmp_path):
    path = _make_db(tmp_path)
    _insert(path, "A", "A-current", version=2, emb_text="A-OLD")  # stale (v>1)
    _insert(path, "B", "B-current", version=2, emb_text="B-current")  # fresh (v>1)
    _insert(path, "C", "C-current", version=1, emb_text="C-OLD")  # v1 -> ignored

    result = sr.reembed_stale_procedure_embeddings(path, provider=_FakeProvider())

    assert result == {"targeted": 2, "stale": 1, "unresolvable": 0, "reembedded": 1}
    assert _emb_of(path, "A") == pack_embedding(_fake_vec("A-current"))  # healed
    assert _emb_of(path, "B") == pack_embedding(_fake_vec("B-current"))  # unchanged
    assert _emb_of(path, "C") == pack_embedding(_fake_vec("C-OLD"))  # untouched (v1)


def test_null_embedding_on_multiversion_row_is_healed(tmp_path):
    path = _make_db(tmp_path)
    _insert(path, "N", "N-current", version=3, emb_text=None)  # NULL blob, v>1

    result = sr.reembed_stale_procedure_embeddings(path, provider=_FakeProvider())

    assert result == {"targeted": 1, "stale": 1, "unresolvable": 0, "reembedded": 1}
    assert _emb_of(path, "N") == pack_embedding(_fake_vec("N-current"))


def test_skips_deprecated_multiversion_row(tmp_path):
    path = _make_db(tmp_path)
    _insert(path, "D", "D-current", version=5, emb_text="D-OLD", deprecated=1)

    result = sr.reembed_stale_procedure_embeddings(path, provider=_FakeProvider())
    assert result == {"targeted": 0, "stale": 0, "unresolvable": 0, "reembedded": 0}
    assert _emb_of(path, "D") == pack_embedding(_fake_vec("D-OLD"))


def test_heals_quarantined_multiversion_row(tmp_path):
    # Quarantine is reversible → the row can rejoin the match set → heal it.
    path = _make_db(tmp_path)
    _insert(path, "Q", "Q-current", version=4, emb_text="Q-OLD", quarantined=1)

    result = sr.reembed_stale_procedure_embeddings(path, provider=_FakeProvider())
    assert result == {"targeted": 1, "stale": 1, "unresolvable": 0, "reembedded": 1}
    assert _emb_of(path, "Q") == pack_embedding(_fake_vec("Q-current"))


def test_verify_zero_and_idempotent_after_heal(tmp_path):
    path = _make_db(tmp_path)
    _insert(path, "A", "A-current", version=2, emb_text="A-OLD")
    provider = _FakeProvider()

    sr.reembed_stale_procedure_embeddings(path, provider=provider)

    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        assert sr.count_stale_procedure_embeddings(con, provider=provider) == 0
    finally:
        con.close()
    again = sr.reembed_stale_procedure_embeddings(path, provider=provider)
    assert again == {"targeted": 1, "stale": 0, "unresolvable": 0, "reembedded": 0}


def test_noop_when_no_multiversion_rows_needs_no_embedder(tmp_path):
    path = _make_db(tmp_path)
    _insert(path, "C", "C-current", version=1, emb_text="C-OLD")  # only a v1 row

    # provider=None must NOT raise: with no candidates the embedder is never used.
    result = sr.reembed_stale_procedure_embeddings(path, provider=None)
    assert result == {"targeted": 0, "stale": 0, "unresolvable": 0, "reembedded": 0}


class _UnavailableProvider:
    async def embed(self, text):
        from genesis.memory.embeddings import EmbeddingUnavailableError

        raise EmbeddingUnavailableError("backends down")


def test_fail_closed_when_embedder_unavailable(tmp_path, monkeypatch):
    path = _make_db(tmp_path)
    _insert(path, "A", "A-current", version=2, emb_text="A-OLD")  # a real candidate
    monkeypatch.setattr(sr, "_EMBED_RETRY_BASE_DELAY_S", 0.0)  # don't sleep between retries
    # No injected provider -> the migration constructs a local EmbeddingProvider;
    # simulate the backend being down so embed() raises.
    monkeypatch.setattr(
        "genesis.memory.embeddings.EmbeddingProvider",
        lambda *a, **k: _UnavailableProvider(),
    )

    with pytest.raises(RuntimeError, match="embedder unavailable"):
        sr.reembed_stale_procedure_embeddings(path, provider=None)
    assert _emb_of(path, "A") == pack_embedding(_fake_vec("A-OLD"))  # no half-heal


class _FlakyProvider:
    """EmbeddingUnavailableError for the first ``fail_times`` embed calls, then
    succeeds — a cold-boot network blip (DNS/egress not warm) that clears on retry."""

    def __init__(self, fail_times: int):
        self.fail_times = fail_times
        self.attempts = 0

    async def embed(self, text: str) -> list[float]:
        from genesis.memory.embeddings import EmbeddingUnavailableError

        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise EmbeddingUnavailableError("cold boot: backends not warm yet")
        return _fake_vec(text)


def test_embed_retries_then_succeeds_on_transient(tmp_path, monkeypatch):
    # A transient cold-boot embedder failure must NOT fail the one-time heal: the
    # bounded retry recovers once the backend warms.
    monkeypatch.setattr(sr, "_EMBED_RETRY_BASE_DELAY_S", 0.0)  # no real sleeps
    path = _make_db(tmp_path)
    _insert(path, "A", "A-current", version=2, emb_text="A-OLD")
    provider = _FlakyProvider(fail_times=2)  # fails twice, third attempt succeeds

    result = sr.reembed_stale_procedure_embeddings(path, provider=provider)

    assert result == {"targeted": 1, "stale": 1, "unresolvable": 0, "reembedded": 1}
    assert provider.attempts == 3  # two failures + one success
    assert _emb_of(path, "A") == pack_embedding(_fake_vec("A-current"))  # healed


def test_embed_raises_dependency_unavailable_after_exhausting_retries(tmp_path, monkeypatch):
    from genesis.db.data_migrations._util import MigrationDependencyUnavailable

    monkeypatch.setattr(sr, "_EMBED_RETRY_BASE_DELAY_S", 0.0)
    monkeypatch.setattr(sr, "_EMBED_ATTEMPTS", 2)
    path = _make_db(tmp_path)
    _insert(path, "A", "A-current", version=2, emb_text="A-OLD")

    # A persistently-down embedder raises the sentinel (which is a RuntimeError
    # subclass, so existing `except RuntimeError` dependency-guards still catch it).
    with pytest.raises(MigrationDependencyUnavailable):
        sr.reembed_stale_procedure_embeddings(path, provider=_UnavailableProvider())
    assert issubclass(MigrationDependencyUnavailable, RuntimeError)
    assert _emb_of(path, "A") == pack_embedding(_fake_vec("A-OLD"))  # no half-heal


def test_migration_migrate_then_verify(tmp_path, monkeypatch):
    from genesis.db.data_migrations import d0011_reembed_stale_procedure_embeddings as mig

    path = _make_db(tmp_path)
    _insert(path, "A", "A-current", version=2, emb_text="A-OLD")
    monkeypatch.setattr(mig, "genesis_db_path", lambda: path)
    # migrate() injects no provider -> it constructs a local EmbeddingProvider;
    # patch that construction to our deterministic fake.
    monkeypatch.setattr(
        "genesis.memory.embeddings.EmbeddingProvider", lambda *a, **k: _FakeProvider()
    )

    summary = mig.migrate()
    assert summary == {"targeted": 1, "stale": 1, "unresolvable": 0, "reembedded": 1}
    assert mig.verify() is True
    assert _emb_of(path, "A") == pack_embedding(_fake_vec("A-current"))


class _ConcurrentRefineProvider:
    """Simulates a genuine refine landing on the row DURING the embed phase —
    between the migration's candidate read (T0) and its write."""

    def __init__(self, path, target_id, concurrent_text):
        self.path = path
        self.target_id = target_id
        self.concurrent_text = concurrent_text
        self.fired = False

    async def embed(self, text):
        if not self.fired:
            self.fired = True
            con = sqlite3.connect(self.path)
            con.execute(
                "UPDATE procedural_memory "
                "SET principle_embedding = ?, version = version + 1 WHERE id = ?",
                (pack_embedding(_fake_vec(self.concurrent_text)), self.target_id),
            )
            con.commit()
            con.close()
        return _fake_vec(text)


def test_concurrent_refine_is_not_clobbered(tmp_path):
    # Row is stale at read time, but a concurrent refine writes a FRESH embedding
    # while the migration is embedding. The compare-and-swap write must skip it,
    # leaving the concurrent (correct) embedding intact — not reintroduce the bug.
    path = _make_db(tmp_path)
    _insert(path, "A", "A-current", version=2, emb_text="A-OLD")
    provider = _ConcurrentRefineProvider(path, "A", "A-CONCURRENT")

    result = sr.reembed_stale_procedure_embeddings(path, provider=provider)

    assert result == {"targeted": 1, "stale": 1, "unresolvable": 0, "reembedded": 0}
    # The concurrent write survived; the migration's stale-principle write was skipped.
    assert _emb_of(path, "A") == pack_embedding(_fake_vec("A-CONCURRENT"))


class _BadDimProvider:
    async def embed(self, text):
        return [0.1] * 512  # wrong dimensionality -> pack_embedding raises


def test_bad_dimension_is_unresolvable_and_blocks_verify(tmp_path):
    path = _make_db(tmp_path)
    _insert(path, "A", "A-current", version=2, emb_text="A-OLD")
    provider = _BadDimProvider()

    result = sr.reembed_stale_procedure_embeddings(path, provider=provider)

    # Not healed (won't write a bad blob) and NOT silently dropped: counted as
    # unresolvable, and the stored embedding is left exactly as it was.
    assert result == {"targeted": 1, "stale": 0, "unresolvable": 1, "reembedded": 0}
    assert _emb_of(path, "A") == pack_embedding(_fake_vec("A-OLD"))

    # verify() must NOT pass while an unresolvable row remains.
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        assert sr.count_stale_procedure_embeddings(con, provider=provider) == 1
    finally:
        con.close()
