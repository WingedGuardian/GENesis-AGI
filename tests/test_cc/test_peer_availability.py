"""Tests for the CC roster peer-availability record (genesis.cc.peer_availability).

Two of these are regression locks for defects found in adversarial review of the
first draft, and are commented as such: attribution (a local fault must never be
blamed on the peer) and the scrub ordering (truncating before scrubbing leaked a
secret's prefix to disk).

NOTE ON WHAT IS *NOT* TESTED HERE. An earlier draft asserted that recording a
peer as blocked left ``roster.failover_chain`` unchanged. That test was
tautological: ``roster.py`` contains ZERO references to this module and by design
never will (it is selection-only; the orchestration lives in
``conversation._try_roster_failover``), so it passed whether or not the feature
existed — and would have kept passing if someone added a gate at the real place.
The advisory-not-a-gate property is now tested where it can actually break, in
tests/test_cc/test_conversation_failover.py.
"""

from __future__ import annotations

import json

import pytest

from genesis.cc import peer_availability as PA
from genesis.cc.exceptions import (
    CCMCPError,
    CCNetworkOfflineError,
    CCQuotaExhaustedError,
    CCRateLimitError,
    CCSessionError,
    CCTimeoutError,
)


@pytest.fixture(autouse=True)
def _hermetic_home(tmp_path, monkeypatch):
    # genesis_home() honors GENESIS_HOME — point it at a tmp dir so tests never
    # touch the real ~/.genesis/cc_peer_availability.json.
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
    return tmp_path


# ── attribution: what counts as evidence about a peer ────────────────────────


def test_read_missing_is_empty():
    assert PA.read() == {}
    assert PA.read_peer("peer-x") is None


@pytest.mark.parametrize("exc", [CCRateLimitError("429 limit"), CCQuotaExhaustedError("cap")])
def test_provider_refusal_is_recorded(exc):
    assert PA.note_failure("peer-x", exc) is True
    st = PA.read_peer("peer-x")
    assert st.available is False
    assert st.reason == PA.QUOTA
    assert st.observed_at


@pytest.mark.parametrize(
    "exc",
    [
        CCNetworkOfflineError("no route to host"),
        CCTimeoutError("our own 7200s timeout"),
        CCMCPError("a Genesis MCP server died"),
        CCSessionError("stale sticky session id"),
    ],
)
def test_local_fault_is_never_blamed_on_the_peer(exc):
    """REGRESSION LOCK (review blocker B1).

    These are all CCError on the same failover branch, but none of them is an
    answer FROM the provider — the offline case never puts a packet on the wire.
    An earlier draft recorded them, so a single local blip marked the entire
    standby fleet unavailable: a confident, wrong "all backups down", which is
    the precise failure this module exists to prevent.
    """
    assert PA.note_failure("peer-x", exc) is False
    assert PA.read_peer("peer-x") is None


def test_success_clears_a_prior_block():
    PA.note_failure("peer-x", CCRateLimitError("429"))
    assert PA.note_success("peer-x") is True
    st = PA.read_peer("peer-x")
    assert st.available is True
    assert st.reason == ""
    assert st.detail == ""
    assert st.reset_at == ""


def test_peers_are_tracked_independently():
    PA.note_failure("peer-a", CCRateLimitError("429"))
    PA.note_success("peer-b")
    peers = PA.read()
    assert peers["peer-a"].available is False
    assert peers["peer-b"].available is True


def test_empty_peer_name_is_ignored():
    assert PA.note_failure("", CCRateLimitError("429")) is False
    assert PA.read() == {}


# ── secret handling ──────────────────────────────────────────────────────────


def test_secret_straddling_the_truncation_bound_is_still_scrubbed(monkeypatch):
    """REGRESSION LOCK (review blocker B3).

    The first draft truncated to _MAX_TEXT and THEN scanned the environment, so a
    secret crossing the bound lost its tail, no longer matched, and its head was
    written to disk verbatim (reproduced: a 64-char token at offset 290 leaked a
    10-char prefix). This file is persisted and lands in backups, so a partial
    prefix is a real leak. Scrub must happen BEFORE truncation.
    """
    secret = "TOKENVALUE-" + ("z" * 53)  # 64 chars, straddles the 300-char bound
    monkeypatch.setenv("SOME_API_KEY", secret)
    PA.note_failure("peer-x", CCRateLimitError(("a" * 290) + secret))
    raw = PA._state_path().read_text()
    assert secret not in raw
    for n in (10, 16, 24):
        assert secret[:n] not in raw, f"leaked a {n}-char prefix of the secret"


def test_only_credential_named_env_values_are_scrubbed(monkeypatch):
    """Filtering by value LENGTH alone also redacts PWD / VIRTUAL_ENV /
    ANTHROPIC_BASE_URL — which would blank out the only human-readable field
    exactly when a connection error is what you need to read."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.example.invalid/anthropic")
    monkeypatch.setenv("SOME_API_KEY", "SECRETVALUE-abcdefghijklmnop")
    PA.note_failure(
        "peer-x",
        CCRateLimitError(
            "429 from https://api.example.invalid/anthropic using SECRETVALUE-abcdefghijklmnop"
        ),
    )
    detail = PA.read_peer("peer-x").detail
    assert "https://api.example.invalid/anthropic" in detail  # readable
    assert "SECRETVALUE-abcdefghijklmnop" not in detail  # redacted
    assert "<redacted>" in detail


def test_short_env_values_are_not_scrubbed(monkeypatch):
    monkeypatch.setenv("SHORT_KEY", "1")
    PA.note_failure("peer-x", CCRateLimitError("rejected with code 1"))
    assert "code 1" in PA.read_peer("peer-x").detail


def test_detail_is_bounded_after_scrubbing():
    PA.note_failure("peer-x", CCRateLimitError("x" * 5000))
    assert len(PA.read_peer("peer-x").detail) <= PA._MAX_TEXT


# ── reset parsing (reuses genesis.cc.rate_limit_reset) ───────────────────────


def test_reset_at_is_populated_when_the_provider_hint_is_parseable():
    exc = CCRateLimitError(
        "limit", raw_text="Claude usage limit reached · resets 4:10am (America/Los_Angeles)"
    )
    PA.note_failure("peer-x", exc)
    assert PA.read_peer("peer-x").reset_at  # a real timestamp resolved


def test_reset_at_is_empty_when_the_hint_is_ambiguous():
    """MEASURED: the shared parser returns None for a bare local datetime with no
    timezone. Leaving reset_at empty is correct — inventing a zone would put
    false precision into a record a human reads to decide if the backup is back."""
    exc = CCRateLimitError(
        "429",
        raw_text="Usage limit reached for 5 hour. Your limit will reset at 2026-09-03 06:19:40",
    )
    PA.note_failure("peer-x", exc)
    assert PA.read_peer("peer-x").reset_at == ""


# ── robustness of the file itself ────────────────────────────────────────────


def test_corrupt_file_reads_empty(tmp_path):
    (tmp_path / "cc_peer_availability.json").write_text("not-json{")
    assert PA.read() == {}


def test_non_dict_json_reads_empty(tmp_path):
    (tmp_path / "cc_peer_availability.json").write_text("[1, 2, 3]")
    assert PA.read() == {}


def test_missing_peers_key_reads_empty(tmp_path):
    (tmp_path / "cc_peer_availability.json").write_text(json.dumps({"other": 1}))
    assert PA.read() == {}


def test_non_utf8_file_does_not_raise(tmp_path):
    """`read()` promises it never raises — it sits on the failover path. Decoding
    with strict UTF-8 broke that promise on a corrupt/binary file."""
    (tmp_path / "cc_peer_availability.json").write_bytes(b"\xff\xfe\x00binary")
    assert PA.read() == {}


def test_oversized_detail_from_disk_is_rebounded_on_read(tmp_path):
    """A file written by another build must not push an unbounded blob into the
    health snapshot (and from there into an LLM context via the health MCP tool)."""
    (tmp_path / "cc_peer_availability.json").write_text(
        json.dumps({"peers": {"p": {"available": False, "detail": "y" * 50_000}}})
    )
    assert len(PA.read_peer("p").detail) <= PA._MAX_TEXT


def test_peer_count_is_capped():
    """Nothing removes a renamed/rotated peer id, and the file is read whole into
    every snapshot — so growth must be bounded."""
    for i in range(PA._MAX_PEERS + 10):
        PA.note_failure(f"peer-{i:03d}", CCRateLimitError("429"))
    assert len(PA.read()) == PA._MAX_PEERS
    # Eviction is least-recently-observed, so the newest survives.
    assert f"peer-{PA._MAX_PEERS + 9:03d}" in PA.read()


# ── round-2 review fixes ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "wording",
    [
        "API Error (HTTP 402): insufficient balance",
        '{"error":{"code":"1113","message":"insufficient balance"}}',
        "API Error (HTTP 402): Insufficient credits",
    ],
)
def test_drained_prepaid_peer_is_recorded(wording):
    """REGRESSION LOCK: the most common way a roster peer becomes unusable.

    A third-party endpoint refuses a drained account with "insufficient
    balance", not "429". Before the classifier learned that family it fell
    through to CCProcessError, note_failure declined it, and a peer that was out
    of credit stayed invisible — the module failing at its own purpose.
    """
    from genesis.cc.invoker import CCInvoker

    exc = CCInvoker._classify_error(wording)
    assert PA._is_provider_refusal(exc), f"{type(exc).__name__} not treated as a refusal"
    assert PA.note_failure("peer-x", exc) is True
    assert PA.read_peer("peer-x").reason == PA.QUOTA


def test_auth_error_is_not_treated_as_a_quota_refusal():
    """Scope boundary, asserted so it stays deliberate: a bad key is a config
    fault with a different remedy, not the provider reporting its own capacity."""
    from genesis.cc.invoker import CCInvoker

    assert PA._is_provider_refusal(CCInvoker._classify_error("401 invalid api key")) is False


def test_a_corrupt_row_does_not_permanently_kill_recording(tmp_path):
    """REGRESSION LOCK: one non-dict row used to end recording FOREVER.

    _record merged the raw on-disk map, so the eviction sort hit `str.get` on a
    non-dict row and raised; the except swallowed it and note_failure returned
    False on every subsequent call — a silent, permanent loss of the signal.
    """
    (tmp_path / "cc_peer_availability.json").write_text(
        json.dumps({"peers": {"bad": "not-a-dict"}})
    )
    for i in range(PA._MAX_PEERS + 2):
        PA.note_failure(f"p{i}", CCRateLimitError("429"))
    assert PA.note_failure("final", CCRateLimitError("429")) is True
    assert PA.read_peer("final") is not None


def test_oversized_row_from_disk_is_rebounded_when_writing(tmp_path):
    """_MAX_TEXT bounds the value being written; a blob left by another build
    must not be re-persisted verbatim on the next record."""
    (tmp_path / "cc_peer_availability.json").write_text(
        json.dumps({"peers": {"old": {"available": False, "detail": "y" * 50_000}}})
    )
    PA.note_failure("new", CCRateLimitError("429"))
    assert len(PA._state_path().read_text()) < 5_000


def test_roster_declared_auth_env_is_scrubbed_even_without_a_hint_word(monkeypatch):
    """A peer's own token is the credential most likely to appear in that peer's
    error text, and auth_env names are user-chosen — `GLM_PAT` matches no hint."""
    token = "PEERTOKEN-abcdefghijklmnop"
    monkeypatch.setenv("GLM_PAT", token)
    monkeypatch.setattr(
        PA, "_secret_names",
        lambda: {"GLM_PAT"},  # stand in for the roster declaring auth_env: GLM_PAT
    )
    PA.note_failure("peer-x", CCRateLimitError(f"429 rejected token {token}"))
    assert token not in PA._state_path().read_text()


def test_longest_secret_is_redacted_first(monkeypatch):
    """When one credential contains another, redacting the shorter first would
    leave the longer one's tail behind as an unmatched fragment."""
    short = "TOKENPREFIX-1234"
    long = short + "-EXTENDEDTAILVALUE"
    monkeypatch.setenv("A_KEY", short)
    monkeypatch.setenv("B_KEY", long)
    PA.note_failure("peer-x", CCRateLimitError(f"429 using {long}"))
    detail = PA.read_peer("peer-x").detail
    assert "EXTENDEDTAILVALUE" not in detail
    assert long not in detail


# ── the snapshot surface (what an operator/LLM actually reads) ───────────────


def test_snapshot_reports_blocked_first_with_age_and_no_current_state_claim():
    from genesis.observability.snapshots.cc_sessions import _peer_availability_snapshot

    PA.note_success("healthy-peer")
    PA.note_failure("blocked-peer", CCRateLimitError("429 limit reached"))
    rows = _peer_availability_snapshot()

    assert [r["peer"] for r in rows] == ["blocked-peer", "healthy-peer"]  # blocked first
    blocked = rows[0]
    assert blocked["available"] is False
    assert blocked["reason"] == PA.QUOTA
    assert isinstance(blocked["age_seconds"], int) and blocked["age_seconds"] >= 0
    assert set(blocked) == {
        "peer", "available", "reason", "observed_at",
        "age_seconds", "detail", "reset_at", "limit_kind",
    }


def test_snapshot_age_is_none_for_an_unparseable_stamp(tmp_path):
    """None, never 0 — a garbage stamp must not read as 'just observed'."""
    from genesis.observability.snapshots.cc_sessions import _peer_availability_snapshot

    (tmp_path / "cc_peer_availability.json").write_text(
        json.dumps({"peers": {"p": {"available": False, "observed_at": "not-a-date"}}})
    )
    assert _peer_availability_snapshot()[0]["age_seconds"] is None


def test_snapshot_is_empty_and_silent_with_no_records():
    from genesis.observability.snapshots.cc_sessions import _peer_availability_snapshot

    assert _peer_availability_snapshot() == []


# ── review round 3 (PR #1646) ────────────────────────────────────────────────


def test_malformed_timestamps_do_not_evict_the_record_being_written(tmp_path):
    """REGRESSION LOCK: eviction ranked by the RAW string, so a row carrying
    observed_at="zzzz" outranked every real ISO stamp by ASCII. Once _MAX_PEERS
    such rows existed, each genuine new observation was evicted the instant it
    was written — note_failure returned True while read_peer found nothing, and
    recording stayed broken until someone deleted the file by hand."""
    poison = {
        f"junk-{i}": {"available": False, "observed_at": "zzzz"}
        for i in range(PA._MAX_PEERS + 5)
    }
    (tmp_path / "cc_peer_availability.json").write_text(json.dumps({"peers": poison}))

    assert PA.note_failure("real-peer", CCRateLimitError("429")) is True
    st = PA.read_peer("real-peer")
    assert st is not None, "the row just written was evicted by malformed rows"
    assert st.available is False
    assert len(PA.read()) <= PA._MAX_PEERS


def test_record_reports_false_when_the_write_fails(monkeypatch):
    """A caller told an observation was recorded when it was not is exactly the
    confident-but-wrong signal this module exists to remove."""
    monkeypatch.setattr(PA, "_write", lambda payload: False)
    assert PA.note_failure("peer-x", CCRateLimitError("429")) is False


def test_failed_write_leaves_no_orphaned_temp_file(tmp_path, monkeypatch):
    """A failure between mkstemp and os.replace must not accumulate .tmp files
    next to the state it could not write (disk-full is the realistic trigger)."""
    def _boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(PA.os, "replace", _boom)
    assert PA.note_failure("peer-x", CCRateLimitError("429")) is False
    assert not list(tmp_path.glob("*.tmp")), "orphaned temp file left behind"


def test_balance_refusal_is_recorded_without_widening_global_classification():
    """The drained-account phrases live HERE, not in the invoker's quota table.

    Teaching the global classifier these phrases would let a drained BACKUP
    relay an account-wide CCStatus.UNAVAILABLE about the primary, and would
    outrank the invoker's MCP classification (an MCP tool whose own error text
    says "insufficient funds" would be parked as a provider quota failure).
    """
    from genesis.cc.exceptions import CCProcessError
    from genesis.cc.invoker import CCInvoker

    exc = CCInvoker._classify_error("API Error (HTTP 402): insufficient balance")
    # Global classification is deliberately UNCHANGED by this PR...
    assert isinstance(exc, CCProcessError)
    assert not isinstance(exc, (CCRateLimitError, CCQuotaExhaustedError))
    # ...while the advisory record still recognises it as a refusal.
    assert PA._is_provider_refusal(exc) is True
    assert PA.note_failure("peer-x", exc) is True
    assert PA.read_peer("peer-x").reason == PA.QUOTA


def test_an_mcp_error_mentioning_funds_is_not_a_peer_refusal():
    """The exact false positive that keeping these phrases out of the global
    classifier prevents — asserted so a future 'simplification' cannot quietly
    move them back."""
    from genesis.cc.exceptions import CCMCPError

    assert PA._is_provider_refusal(
        CCMCPError("MCP server 'payments' returned error: insufficient funds")
    ) is False


# ── review round 4 (PR #1646) — eviction redesigned, write hardened ──────────


def test_mixed_naive_and_aware_timestamps_do_not_break_recording(tmp_path):
    """REGRESSION LOCK: a parsed-timestamp sort raised TypeError the moment a
    legacy NAIVE stamp met an aware one, and the failure was swallowed — every
    later observation returned False and went unrecorded. Eviction no longer
    parses timestamps at all, so mixed shapes are simply data."""
    rows = {}
    for i in range(PA._MAX_PEERS + 3):
        stamp = "2026-01-01T00:00:00" if i % 2 else "2026-01-01T00:00:00+00:00"
        rows[f"legacy-{i}"] = {"available": False, "observed_at": stamp}
    (tmp_path / "cc_peer_availability.json").write_text(json.dumps({"peers": rows}))

    assert PA.note_failure("fresh", CCRateLimitError("429")) is True
    assert PA.read_peer("fresh") is not None
    assert len(PA.read()) <= PA._MAX_PEERS


def test_state_file_round_trips_as_valid_json(tmp_path):
    """Sanity check on the published file, NOT a regression lock.

    Stated honestly because it was previously claimed as one: this asserts the
    written file parses, but it would pass identically against the old bare
    `os.write`, since a ~2KB payload never short-writes in practice. The
    short-write fix (writing through a file object, which loops or raises) is
    real but is not what this test proves — no cheap probe forces a partial
    write, and a test that cannot fail against the old code is not a lock.
    """
    for i in range(5):
        PA.note_failure(f"peer-{i}", CCRateLimitError("429 " + "x" * 200))
    raw = PA._state_path().read_text()
    parsed = json.loads(raw)  # must not raise
    assert isinstance(parsed.get("peers"), dict)
    assert len(parsed["peers"]) == 5


def test_concurrent_recorders_do_not_drop_each_others_rows(tmp_path):
    """Read-modify-write is serialized across writers. Unserialized, two
    recorders writing DIFFERENT peers in the same instant each read the map, each
    add a row, and the second write drops the first — losing exactly the
    observation this module exists to preserve."""
    import threading

    names = [f"peer-{i:02d}" for i in range(12)]
    barrier = threading.Barrier(len(names))

    def _writer(name: str) -> None:
        barrier.wait()  # maximise overlap on the read-modify-write
        PA.note_failure(name, CCRateLimitError("429"))

    threads = [threading.Thread(target=_writer, args=(n,)) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    recorded = set(PA.read())
    assert recorded == set(names), f"lost {set(names) - recorded}"


# ── round-5: the three BLOCKERs from the full-diff audit ────────────────────


def test_every_field_and_the_key_are_bounded_not_just_detail(tmp_path):
    """BLOCKER: _MAX_TEXT bounded 1 of 7 fields, and nothing bounded the KEY.

    A foreign row was re-persisted whole and handed to the snapshot untouched —
    measured at a 300KB state file and a 100,000-char `reason` reaching an LLM
    through the health MCP tool. The previous test passed only because it seeded
    the one field that WAS bounded, which is how this survived a round.
    """
    (tmp_path / "cc_peer_availability.json").write_text(json.dumps({"peers": {
        "K" * 50_000: {
            "available": False,
            "reason": "R" * 100_000,
            "observed_at": "O" * 100_000,
            "detail": "D" * 100_000,
            "reset_at": "T" * 100_000,
            "limit_kind": "L" * 100_000,
        },
    }}))
    st = next(iter(PA.read().values()))
    for field in ("reason", "observed_at", "detail", "reset_at", "limit_kind"):
        assert len(getattr(st, field)) <= PA._MAX_TEXT, f"{field} unbounded on read"
    assert len(st.peer) <= PA._MAX_PEER_NAME, "peer key unbounded on read"

    # ...and the bound must survive a write, not just a read.
    PA.note_failure("fresh", CCRateLimitError("429"))
    assert len(PA._state_path().read_text()) < 10_000


def test_re_recording_a_peer_makes_it_newest_not_oldest(tmp_path):
    """BLOCKER: assigning an existing dict key does NOT reorder it, so insertion
    order decayed to FIRST-seen and eviction dropped the most recently observed
    peer while keeping stale ones."""
    for i in range(PA._MAX_PEERS):
        PA.note_failure(f"old-{i:03d}", CCRateLimitError("429"))
    PA.note_success("old-000")          # the freshest observation
    PA.note_failure("newcomer", CCRateLimitError("429"))  # forces one eviction

    peers = PA.read()
    assert "old-000" in peers, "evicted the most recently observed peer"
    assert "newcomer" in peers
    assert len(peers) <= PA._MAX_PEERS


def test_lock_acquisition_is_bounded_not_blocking(tmp_path):
    """BLOCKER: a bare LOCK_EX stalled the asyncio event loop — measured at 1.2s
    while another holder ran. This path is reached synchronously from an async
    failover turn, so advisory bookkeeping could delay every concurrent
    conversation during the very outage it exists to observe."""
    import fcntl
    import time

    lock_path = PA._state_path().parent / PA._LOCK_FILE
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = lock_path.open("a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    try:
        start = time.monotonic()
        assert PA.note_failure("peer-x", CCRateLimitError("429")) is True
        elapsed = time.monotonic() - start
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()

    # Bounded by _LOCK_ATTEMPTS * _LOCK_SLEEP_S, then degrades OPEN and records.
    budget = PA._LOCK_ATTEMPTS * PA._LOCK_SLEEP_S
    assert elapsed < budget * 3, f"blocked {elapsed:.2f}s against a {budget:.2f}s budget"
    assert PA.read_peer("peer-x") is not None, "degrade-open must still record"
