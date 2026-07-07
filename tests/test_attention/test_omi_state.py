"""Tests for the OMI advisory state store (dedup keys + per-uid anchor).

The store is ADVISORY: losing it costs at most one duplicate row or ~2s of ts
jitter, never real speech. All TTL/age logic is driven by an injected ``now`` so
the tests never touch the wall clock.
"""
import pytest

from genesis.attention.omi_state import (
    IDEMPOTENCY_TTL_S,
    SEEN_SEGMENTS_TTL_S,
    OmiState,
)


@pytest.fixture()
def state(tmp_path):
    st = OmiState(tmp_path / "omi_state.db")
    yield st
    st.close()


# ── idempotency (delivery-level) dedup ─────────────────────────────────────
def test_idempotency_first_seen_is_not_duplicate_then_is(state):
    assert state.is_duplicate_delivery("key-1", now=1000.0) is False
    assert state.is_duplicate_delivery("key-1", now=1000.5) is True


def test_idempotency_none_key_never_duplicate(state):
    assert state.is_duplicate_delivery(None, now=1000.0) is False
    assert state.is_duplicate_delivery(None, now=1001.0) is False


def test_idempotency_key_expires_after_ttl(state):
    state.is_duplicate_delivery("key-1", now=1000.0)
    # Just past the TTL: the old key is pruned, so it reads as fresh again.
    assert state.is_duplicate_delivery("key-1", now=1000.0 + IDEMPOTENCY_TTL_S + 1) is False


# ── segment-uuid dedup ─────────────────────────────────────────────────────
def test_seen_segment_ids_empty_initially(state):
    assert state.seen_segment_ids(["a", "b"], now=1000.0) == set()


def test_record_then_seen(state):
    state.record_segment_ids(["a", "b"], now=1000.0)
    assert state.seen_segment_ids(["a", "b", "c"], now=1000.5) == {"a", "b"}


def test_record_is_idempotent(state):
    state.record_segment_ids(["a"], now=1000.0)
    state.record_segment_ids(["a"], now=1001.0)  # must not raise
    assert state.seen_segment_ids(["a"], now=1001.0) == {"a"}


def test_seen_ignores_none_ids(state):
    state.record_segment_ids([None, "a"], now=1000.0)
    assert state.seen_segment_ids([None, "a"], now=1000.0) == {"a"}


def test_seen_segment_expires_after_ttl(state):
    state.record_segment_ids(["a"], now=1000.0)
    assert state.seen_segment_ids(["a"], now=1000.0 + SEEN_SEGMENTS_TTL_S + 1) == set()


# ── per-uid anchor ─────────────────────────────────────────────────────────
def test_anchor_unknown_uid_is_none(state):
    assert state.get_anchor("nobody") is None


def test_anchor_roundtrip(state):
    state.set_anchor("uid-1", epoch0=995.0, max_end=10.0, now=1005.0)
    assert state.get_anchor("uid-1") == (995.0, 10.0)


def test_anchor_upsert_overwrites(state):
    state.set_anchor("uid-1", epoch0=995.0, max_end=10.0, now=1005.0)
    state.set_anchor("uid-1", epoch0=8998.0, max_end=2.0, now=9000.0)
    assert state.get_anchor("uid-1") == (8998.0, 2.0)


def test_advisory_loss_reanchors(tmp_path):
    # Simulate state loss: a brand-new store on a fresh path has no anchor,
    # so the next batch re-anchors sanely rather than crashing.
    st1 = OmiState(tmp_path / "s.db")
    st1.set_anchor("uid-1", epoch0=995.0, max_end=10.0, now=1005.0)
    st1.close()
    (tmp_path / "s.db").unlink()
    st2 = OmiState(tmp_path / "s.db")
    assert st2.get_anchor("uid-1") is None
    st2.close()
