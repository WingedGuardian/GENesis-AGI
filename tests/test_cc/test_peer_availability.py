"""Tests for the CC roster peer-availability record (genesis.cc.peer_availability).

One of these is a regression lock for a defect found in adversarial review of the
first draft, and is commented as such: attribution — a local fault must never be
blamed on the peer. (The scrub-ordering lock is gone with the scrub itself: the
record no longer stores free text, so there is no secret to order anything
around. See ``test_generator_B_is_structurally_absent_no_credential_discovery``,
which asserts the mechanism cannot come back.)

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
from datetime import UTC, datetime

import pytest

from genesis.cc import peer_availability as PA
from genesis.cc.exceptions import (
    CCMCPError,
    CCNetworkOfflineError,
    CCProcessError,
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
    # The read-path complaint suppressor is deliberate module state (see
    # `_last_read_complaint`), so it survives between tests and would make any
    # warning assertion depend on execution ORDER — a leaked-state false green,
    # and the hardest kind to attribute. Reset it with the rest of the world.
    monkeypatch.setattr(PA, "_last_read_complaint", {})
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


# ── the snapshot surface (what an operator/LLM actually reads) ───────────────


async def test_snapshot_reports_blocked_first_with_age_and_no_current_state_claim():
    from genesis.observability.snapshots.cc_sessions import _peer_availability_snapshot

    PA.note_success("healthy-peer")
    PA.note_failure("blocked-peer", CCRateLimitError("429 limit reached"))
    rows = await _peer_availability_snapshot()

    assert [r["peer"] for r in rows] == ["blocked-peer", "healthy-peer"]  # blocked first
    blocked = rows[0]
    assert blocked["available"] is False
    assert blocked["reason"] == PA.QUOTA
    assert isinstance(blocked["age_seconds"], int) and blocked["age_seconds"] >= 0
    assert set(blocked) == {
        "peer", "available", "reason", "observed_at",
        "age_seconds", "reset_at", "limit_kind",
    }


async def test_snapshot_is_empty_and_silent_with_no_records():
    from genesis.observability.snapshots.cc_sessions import _peer_availability_snapshot

    assert await _peer_availability_snapshot() == []


# ── review round 3 (PR #1646) ────────────────────────────────────────────────


def _valid_row(**over):
    """A row through the ACCEPT set, so fixtures actually land in the store.

    Two eviction tests used to seed rows the decoder rejects outright, so they
    ran against an EMPTY store and passed with the eviction logic deleted —
    MEASURED at 35 rows written, 0 decoded. Anything asserting on eviction has
    to build rows that survive `_decode_row` first.
    """
    row = {
        "available": False,
        "reason": "quota",
        "observed_at": datetime.now(UTC).isoformat(),
        "reset_at": "",
        "limit_kind": "unknown",
    }
    row.update(over)
    return row


def test_a_full_store_does_not_evict_the_row_just_written(tmp_path):
    """REGRESSION LOCK, rebuilt so it can fail.

    The original defect: eviction ranked by the RAW timestamp string, so a row
    carrying observed_at="zzzz" outranked every real ISO stamp by ASCII, and once
    _MAX_PEERS of them existed each genuine observation was evicted the instant it
    was written — note_failure returned True while read_peer found nothing.

    That exact fixture is now unreachable (a malformed stamp is rejected, so it
    never reaches eviction at all), which is why the test is rebuilt on VALID
    rows rather than deleted: the property it guards — a full store must not
    swallow the newest write — is still real, and eviction is still by insertion
    order with the new row re-inserted last.
    """
    full = {f"old-{i:03d}": _valid_row() for i in range(PA._MAX_PEERS + 5)}
    (tmp_path / "cc_peer_availability.json").write_text(json.dumps({"peers": full}))
    assert len(PA.read()) == PA._MAX_PEERS, "fixture must actually populate the store"

    assert PA.note_failure("real-peer", CCRateLimitError("429")) is True
    st = PA.read_peer("real-peer")
    assert st is not None, "the row just written was evicted by a full store"
    assert st.available is False
    # Assert on the FILE, not on read(): the read-side cap also trims to
    # _MAX_PEERS, so checking read() alone passes even with WRITE-side eviction
    # deleted. Found by mutation — a sibling layer masking the mechanism.
    on_disk = json.loads(PA._state_path().read_text())["peers"]
    assert len(on_disk) == PA._MAX_PEERS, "write-side eviction did not run"
    assert "real-peer" in on_disk


def test_a_naive_timestamp_is_rejected_not_stored(tmp_path):
    """Replaces a mixed-naive/aware test whose premise the decoder removed.

    `_record` only ever writes `datetime.now(UTC).isoformat()`, so a naive stamp
    is not something this module emits. It round-trips through `fromisoformat`
    perfectly, though, so the round-trip check alone accepted it — and the
    snapshot then subtracted an aware `now` from it, suppressed the TypeError and
    reported `age_seconds: null`. A blocked peer with no staleness is exactly
    what a reader takes as current state.
    """
    (tmp_path / "cc_peer_availability.json").write_text(json.dumps({"peers": {
        "naive": _valid_row(observed_at="2026-09-04T00:00:00"),
        "aware": _valid_row(),
    }}))
    assert PA.read_peer("naive") is None, "accepted a stamp we never write"
    assert PA.read_peer("aware") is not None, "rejected a stamp we do write"


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


def test_lock_contention_fails_closed_rather_than_clobbering(tmp_path):
    """Contention must NOT write unserialized — that is the data-loss bug.

    The original defect was a bare LOCK_EX stalling the event loop. The fix
    (non-blocking + bounded retry) then "degraded OPEN" on exhaustion and wrote
    anyway, which does not rescue OUR row — it clobbers the holder's. MEASURED
    with 12 concurrent writers at the old 10 x 20ms budget: ~50% of runs lost at
    least one row, because acquisition is a LOTTERY, not a queue.

    Now: contention yields False and nothing is written, and `note_failure`
    reports False honestly. Losing our own observation beats destroying someone
    else's, and the return value already means "the row landed".
    """
    import fcntl
    import time

    lock_path = PA._state_path().parent / PA._LOCK_FILE
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    holder = lock_path.open("a+")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX)
    try:
        monkey_budget = 3  # keep the test fast; the property is the fail DIRECTION
        real = PA._LOCK_ATTEMPTS
        PA._LOCK_ATTEMPTS = monkey_budget
        start = time.monotonic()
        landed = PA.note_failure("peer-x", CCRateLimitError("429"))
        elapsed = time.monotonic() - start
    finally:
        PA._LOCK_ATTEMPTS = real
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()

    assert landed is False, "reported a row landed while the lock was held"
    assert PA.read_peer("peer-x") is None, "wrote unserialized — this clobbers the holder"
    budget = monkey_budget * PA._LOCK_SLEEP_S
    assert elapsed < budget * 5, f"waited {elapsed:.2f}s against a {budget:.2f}s budget"


# ── THE MODULE'S INVARIANTS, AS EXECUTABLE TESTS ─────────────────────────────
#
# The adversarial audit's root cause was "each round added a mechanism without
# re-deriving the module's invariants". Prose did not prevent that — the module
# docstring said "never block the event loop" and a blocking flock was added
# under it anyway. So each invariant below is a test, not a sentence.


def test_invariant_a_storable_name_is_kept_whole_and_distinct():
    """I5. Names that FIT are stored verbatim, and two of them never merge.

    Lengths are derived from the cap, never written as literals: an earlier
    version of this test used a 213-char name, which stopped exercising anything
    the moment the cap moved above it — it passed identically against a build
    that truncated names. A test whose relevance depends on a constant it does
    not read is a test with an expiry date.
    """
    stem = "peer-" + ("p" * (PA._MAX_PEER_NAME - 20))
    blocked, healthy = stem + "-BLOCKED", stem + "-HEALTHY"
    assert max(len(blocked), len(healthy)) <= PA._MAX_PEER_NAME, "fixture must be storable"

    PA.note_failure(blocked, CCRateLimitError("429 quota"))
    PA.note_success(healthy)

    assert PA.read_peer(blocked) is not None, "a storable name was not kept whole"
    assert PA.read_peer(blocked).available is False, "another peer's success cleared this block"
    assert PA.read_peer(healthy).available is True
    assert len({blocked, healthy} & set(PA.read())) == 2, "two peers collapsed onto one key"


def test_invariant_an_unstorable_name_is_dropped_not_truncated():
    """I5. Over the cap, a row is OMITTED — never cut down to a shared prefix.

    `roster.py` applies no length bound to model names (zero `len()` calls), so
    two long names sharing a prefix are possible. Truncating them to fit maps
    both to ONE state key, and a success on one then clears the other's recorded
    quota failure — a correctness bug manufactured by the protective cap. Not
    storing the row is honest; storing it under another peer's identity is not.
    """
    stem = "peer-" + ("p" * (PA._MAX_PEER_NAME + 50))
    blocked, healthy = stem + "-BLOCKED", stem + "-HEALTHY"
    assert min(len(blocked), len(healthy)) > PA._MAX_PEER_NAME, "fixture must be unstorable"

    assert PA.note_failure(blocked, CCRateLimitError("429 quota")) is False, (
        "an unstorable row must report False — True means the row landed"
    )
    PA.note_success(healthy)

    # Nothing was stored, so nothing can be attributed to the wrong peer. Under
    # truncation both would share one key and this map would hold a single row
    # claiming availability for the peer that is actually blocked.
    assert PA.read() == {}, f"an unstorable name reached the store: {list(PA.read())}"


def test_invariant_read_path_enforces_the_peer_cap(tmp_path):
    """I5. The cap must hold on READ, not only on write.

    The file is copied whole into every health snapshot and from there into an
    LLM context via the health MCP tool. Write-side eviction does not help until
    the next observation is recorded — which only happens during a home-model
    outage, so a foreign or older file can inflate every snapshot for days.
    """
    (tmp_path / "cc_peer_availability.json").write_text(json.dumps({"peers": {
        f"peer-{i:04d}": {"available": False, "reason": "quota"}
        for i in range(PA._MAX_PEERS * 20)
    }}))
    assert len(PA.read()) <= PA._MAX_PEERS, "read path ignored the peer cap"


def test_invariant_a_pathological_file_is_not_read_into_memory(tmp_path):
    """I5. Bound the INPUT, not just the parsed result.

    `read()` runs synchronously on the health path. Parsing a multi-hundred-MB
    file to then discard most of it is the memory spike, not the row count —
    and this box is shared with other sessions.
    """
    # Sized FROM the cap, and only just over it: an earlier version wrote ~81MB
    # to test a 1MB bound and asserted against a hard-coded 50MB, which is the
    # same expiry-date anti-pattern this file warns about elsewhere.
    f = tmp_path / "cc_peer_availability.json"
    f.write_text(json.dumps({"peers": {"p" * PA._MAX_STATE_BYTES: {"available": True}}}))
    assert f.stat().st_size > PA._MAX_STATE_BYTES
    assert PA.read() == {}, "an implausibly large state file must be refused, not parsed"


def test_invariant_recording_never_raises_on_a_hostile_exception():
    """I2. This module sits ON the failover path. A raise here escapes into the
    peer loop and abandons every REMAINING peer — turning an observability
    helper into an outage amplifier."""

    class Hostile(CCRateLimitError):
        def __str__(self):
            raise RuntimeError("hostile __str__")

    # True now, and that is an IMPROVEMENT worth stating rather than a
    # regression: the old False came from `detail = str(exc)` raising inside the
    # guard. With no free-text field there is nothing to stringify on this path,
    # so a hostile __str__ no longer costs us the observation. The property under
    # test is unchanged and is the one that matters — it does not RAISE.
    assert PA.note_failure("peer-x", Hostile()) is True
    assert PA.note_success("peer-x") is True


# ── PROOF THAT THE GENERATORS ARE CLOSED ─────────────────────────────────────
#
# Four external review rounds produced 15 findings. Seven were one generator
# (per-field REPAIR of a foreign document) and four were another (inferring
# whether credential discovery had succeeded). Neither was closed by any of the
# fixes, because each fix added a predicate to the mechanism that was generating
# them. These tests assert the mechanisms are GONE, not that the known holes are
# patched — a patched hole is evidence about one input, an absent mechanism is
# evidence about all of them.


def test_generator_B_is_structurally_absent_no_credential_discovery():
    """The scrub/discovery apparatus cannot fail because it no longer exists.

    It needed credential NAMES, which came from the roster, which required
    knowing whether that read had SUCCEEDED — and every way of inferring that
    had a hole, because Genesis config loaders degrade silently by design
    (`roster._load_yaml` swallows and returns {}; a malformed overlay returns the
    BASE, which still carries `models.claude`). Two P1s, three rounds apart.

    Deleting the only free-text field deleted the need. This test fails the
    moment anyone reintroduces it.
    """
    import inspect

    # Check the MECHANISM, not the prose. An earlier version of this test grepped
    # for "redact" and failed on the module docstring that documents the
    # deletion — a test that fires on its own explanation is a bad test.
    for gone in ("_scrub", "_scrub_with", "_secret_names", "_secret_values",
                 "_secret_context", "_bound_text", "_SECRET_NAME_HINTS",
                 "_MAX_DETAIL", "_OMITTED_TEXT", "_OMITTED_UNSAFE"):
        assert not hasattr(PA, gone), f"{gone} is back — so is the generator"

    source = inspect.getsource(PA)
    assert "os.environ" not in source, "reads the environment — the scrub is back"
    assert "from genesis.cc import roster" not in source, "roster coupling is back"

    # And no field of the record may be free text: every one is a closed set.
    fields = {f for f in PA.PeerStatus.__dataclass_fields__}
    assert fields == {"peer", "available", "reason", "observed_at", "reset_at", "limit_kind"}


def test_generator_A_rejects_by_default_over_a_generated_corpus(tmp_path):
    """Enumeration, not a spot-check: no generated document may yield a REPAIRED row.

    The repair layer accepted by default, so every finding was an accept that
    should have been a reject — and each round supplied a value the last one had
    not imagined (naive-vs-aware stamps, unbounded fractional seconds,
    `bool("false") is True`, a truncated name merging two peers). The decoder
    rejects by default, so the accept-set is exactly the vocabulary we emit.
    """
    good_stamp = datetime.now(UTC).isoformat()
    hostile_values = [
        "false", "true", 0, 1, None, [], {}, "", "  ",
        "2026-09-04T00:00:00.123456789+00:00",   # R4: parses, normalises, ≠ input
        "2026-09-04T00:00:00",                    # naive — we never write naive
        "zzzz", "quota ", "QUOTA", "Quota", "session\n",
        "x" * 5000, "\x00", "../../etc/passwd", True,
    ]
    rows = []
    for v in hostile_values:
        for field in ("available", "reason", "observed_at", "reset_at", "limit_kind"):
            base = {"available": False, "reason": "quota", "observed_at": good_stamp,
                    "reset_at": "", "limit_kind": "unknown"}
            base[field] = v
            rows.append(base)
    doc = {"peers": {f"p{i:04d}": r for i, r in enumerate(rows)}}
    (tmp_path / "cc_peer_availability.json").write_text(json.dumps(doc, default=str))

    survivors = PA.read()
    for st in survivors.values():
        # Anything that survived must be EXACTLY what this module emits.
        assert isinstance(st.available, bool)
        assert st.reason in PA._REASONS
        assert st.limit_kind in PA._LIMIT_KINDS
        assert st.observed_at and datetime.fromisoformat(st.observed_at).isoformat() == st.observed_at
        assert st.reset_at == "" or datetime.fromisoformat(st.reset_at).isoformat() == st.reset_at
        assert (st.reason == PA.QUOTA) is (st.available is False)
        assert datetime.fromisoformat(st.observed_at).tzinfo is not None


def test_generator_A_round_trip_everything_we_write_decodes(tmp_path):
    """The other half: rejecting by default is only correct if OUR OWN rows pass.

    Without this the strictness could silently disable the feature — read()
    would return empty forever and every consumer would show a healthy-looking
    nothing, which is the exact blindness the module exists to remove.
    """
    PA.note_failure("blocked-peer", CCRateLimitError(
        "limit", raw_text="Claude usage limit reached · resets 4:10am (America/Los_Angeles)"))
    PA.note_success("healthy-peer")

    on_disk = json.loads(PA._state_path().read_text())["peers"]
    assert set(on_disk) == {"blocked-peer", "healthy-peer"}
    assert set(PA.read()) == {"blocked-peer", "healthy-peer"}, "rejected a row we wrote"

    blocked = PA.read_peer("blocked-peer")
    assert blocked.available is False and blocked.reason == PA.QUOTA
    assert blocked.reset_at, "the parsed reset time must survive the decoder"


@pytest.mark.parametrize("field,bad", [
    ("available", "false"),
    ("reason", "rate-limit"),
    ("observed_at", "2026-09-04T00:00:00.123456789+00:00"),
    ("reset_at", "soon"),
    ("limit_kind", "hourly"),
])
def test_generator_A_mutating_any_field_drops_the_row(tmp_path, field, bad):
    """Per-FIELD proof that the round-trip test above is not vacuous."""
    PA.note_failure("peer-x", CCRateLimitError("429"))
    doc = json.loads(PA._state_path().read_text())
    assert PA.read_peer("peer-x") is not None, "precondition: the row decodes"

    doc["peers"]["peer-x"][field] = bad
    PA._state_path().write_text(json.dumps(doc))
    assert PA.read_peer("peer-x") is None, f"a foreign {field} was repaired instead of rejected"


def test_a_corrupt_row_does_not_take_the_others_with_it(tmp_path):
    """Rejection is per ROW, never per document.

    Discarding the whole file on one bad row would let a single corrupt entry
    destroy every other peer's observation — trading a repair bug for a much
    louder availability bug.
    """
    PA.note_failure("good-1", CCRateLimitError("429"))
    PA.note_success("good-2")
    doc = json.loads(PA._state_path().read_text())
    doc["peers"]["rotten"] = {"available": "false", "reason": "???"}
    PA._state_path().write_text(json.dumps(doc))

    assert set(PA.read()) == {"good-1", "good-2"}


@pytest.mark.parametrize("bad", ["true", "false", 1, 0, "", None, [], "yes"])
def test_available_must_be_a_real_json_boolean(tmp_path, bad):
    """Isolates the bool check from the coherence rule that also happens to catch
    most bad values.

    Found by mutation: replacing the strict check with `bool(available)` SURVIVED,
    because a blocked row's `reason == "quota"` then tripped the coherence rule
    instead. That made the bool check individually unproven. These rows are
    coherent for whichever way truthiness resolves, so only the type check can
    reject them — which is the point, since `bool("false") is True` and a missing
    value fabricated a healthy observation (R4 finding).
    """
    stamp = datetime.now(UTC).isoformat()
    (tmp_path / "cc_peer_availability.json").write_text(json.dumps({"peers": {
        # coherent as HEALTHY if truthiness says True, coherent as BLOCKED if False
        "truthy": {"available": bad, "reason": "", "observed_at": stamp,
                   "reset_at": "", "limit_kind": ""},
    }}))
    assert PA.read_peer("truthy") is None, f"accepted a non-boolean available={bad!r}"


@pytest.mark.parametrize("bad", ["hourly", "SESSION", "weekly ", 5, None, ["session"]])
def test_limit_kind_must_be_a_member_of_the_closed_set(tmp_path, bad):
    """Isolates the limit_kind enum check.

    The coherence rule says nothing about `limit_kind` on a blocked row, so this
    is the only mechanism that can reject these — unlike `reason`, where
    coherence provides a second net and masked the check under mutation.
    """
    stamp = datetime.now(UTC).isoformat()
    (tmp_path / "cc_peer_availability.json").write_text(json.dumps({"peers": {
        "p": {"available": False, "reason": "quota", "observed_at": stamp,
              "reset_at": "", "limit_kind": bad},
    }}))
    assert PA.read_peer("p") is None, f"accepted limit_kind={bad!r}"


def test_reason_must_be_a_member_even_when_coherence_would_pass(tmp_path):
    """Isolates the reason enum check from the coherence rule.

    On an AVAILABLE row the coherence rule only requires `reason` to be falsy, so
    a repaired-to-empty foreign value would slip through it. Only the enum check
    rejects the row outright.
    """
    stamp = datetime.now(UTC).isoformat()
    (tmp_path / "cc_peer_availability.json").write_text(json.dumps({"peers": {
        "p": {"available": True, "reason": "rate-limit", "observed_at": stamp,
              "reset_at": "", "limit_kind": ""},
    }}))
    assert PA.read_peer("p") is None, "a foreign reason was repaired instead of rejected"


def test_public_classifier_never_raises_on_a_hostile_exception():
    """R5-P1. The PUBLIC alias is called from the failover loop's retry gate,
    which sits OUTSIDE ``note_failure``'s guard.

    Splitting classification out of recording (so callers could ask the question
    separately) reintroduced the exact outage-amplifier shape I2 exists to
    prevent: ``_is_provider_refusal`` calls ``str(exc)`` on a foreign exception,
    and an unguarded raise there escapes into the peer loop and abandons every
    REMAINING peer. The guard belongs on the shared entry point, not on one of
    its two callers.
    """

    class Hostile(CCProcessError):
        def __str__(self):
            raise RuntimeError("hostile __str__")

    assert PA.is_provider_refusal(Hostile()) is False


def test_a_persistently_malformed_file_warns_once_not_once_per_read(tmp_path, caplog):
    """R5-P2. ``read()`` runs on EVERY health poll, and a bad row survives until
    some failover happens to rewrite the file — which only occurs during a
    home-model outage.

    So a single malformed row emitted an identical WARNING forever, burying the
    real ones. The drop count is already collapsed to one line per read; the
    missing half is collapsing it across reads. The condition, not the read, is
    what deserves a line.
    """
    f = tmp_path / "cc_peer_availability.json"
    f.write_text(json.dumps({"peers": {
        "good-1": _valid_row(),
        "bad-1": {"available": "yes"},
    }}))

    with caplog.at_level("WARNING", logger="genesis.cc.peer_availability"):
        for _ in range(5):
            assert set(PA.read()) == {"good-1"}, "fixture must drop exactly one row"
    first = [r for r in caplog.records if "unusable peer availability" in r.getMessage()]
    assert len(first) == 1, f"one condition, five reads, {len(first)} warnings"

    # A CHANGED condition is news again — suppression must not silence a file
    # that got worse.
    caplog.clear()
    f.write_text(json.dumps({"peers": {
        "good-1": _valid_row(),
        "bad-1": {"available": "yes"},
        "bad-2": {"available": "no"},
    }}))
    with caplog.at_level("WARNING", logger="genesis.cc.peer_availability"):
        PA.read()
        PA.read()
    second = [r for r in caplog.records if "unusable peer availability" in r.getMessage()]
    assert len(second) == 1, f"a worsened file must re-warn exactly once, got {len(second)}"


# ── SUPPLY, not just the predicate ───────────────────────────────────────────
#
# The tests above hand `is_provider_refusal` an exception they constructed. That
# proves the predicate reads the TYPE; it cannot prove production ever produces
# that type. It did not: `_classify_error` returns CCMCPError — a SIBLING of
# CCProcessError, not a subclass — for any output mentioning MCP, and that branch
# runs BEFORE the generic fallback over the combined stderr+stdout. A drained
# account whose stderr carries an ordinary MCP start-up line was therefore
# invisible to a predicate that matched CCProcessError alone.


@pytest.mark.parametrize("stderr_text,stdout_text", [
    ("API error: insufficient balance", ""),
    ("API Error (HTTP 402): Insufficient credits", ""),
    ("error: insufficient_quota", "step completed"),
])
def test_a_drained_account_is_evidence_when_the_classifier_can_see_it(
    stderr_text, stdout_text,
):
    """Built by routing REAL provider text through the REAL classifier.

    Constructing the exception directly proves only that the predicate reads the
    TYPE; it cannot prove production produces that type. This is the supply side.
    """
    from genesis.cc.invoker import CCInvoker

    exc = CCInvoker._classify_error(stderr_text, stdout_text)
    assert PA.is_provider_refusal(exc) is True, (
        f"a drained account typed as {type(exc).__name__} was not read as evidence"
    )


@pytest.mark.parametrize("stderr_text,stdout_text", [
    ("MCP server 'genesis-health' started\nAPI error: insufficient balance", ""),
    ("API error: insufficient balance", "mcp__genesis-health__health_status"),
])
def test_a_drained_account_is_INVISIBLE_when_mcp_shadows_the_classifier(
    stderr_text, stdout_text,
):
    """A KNOWN COST, pinned as a DECISION rather than left as an accident.

    The rule is now uniform: anything carrying MCP evidence is not evidence about
    the PEER, tested before any type check. That is there for the FALSE-POSITIVE
    direction — a Genesis tool hitting its own 429 was being recorded as the peer
    being quota-blocked (see
    `test_an_mcp_tools_own_ceiling_is_never_the_peers_fault`), which is the
    expensive way to be wrong.

    This test pins what that costs: a GENUINE drained peer whose output happens
    to mention MCP — an ordinary session start-up line — also goes unrecorded.
    That is a missing observation, the cheap direction: the peer is still tried,
    and a human simply is not told. No field separates the two cases; MEASURED
    2026-09-04, `CCMCPError.server_name` is populated for both a real tool
    failure and a drained peer with MCP chatter, so it cannot discriminate.

    An earlier audit proposed widening the predicate to CCMCPError to close this.
    It was tried and reverted — it buys back this observation by paying in false
    positives, which is the trade the wrong way round.

    If this test ever fails, someone has narrowed the MCP exclusion. Read
    `test_an_mcp_tools_own_ceiling_is_never_the_peers_fault` first.
    """
    from genesis.cc.invoker import CCInvoker

    exc = CCInvoker._classify_error(stderr_text, stdout_text)
    assert type(exc).__name__ == "CCMCPError", "the shadowing premise no longer holds"
    assert PA.is_provider_refusal(exc) is False


def test_every_read_path_complaint_is_deduplicated_not_just_the_two_i_noticed(
    tmp_path, caplog,
):
    """The first flood fix covered `dropped` and `over_cap` and left the three
    complaints in `_read_raw` untouched — including the corrupt-JSON line, which
    carries a full traceback on EVERY health poll and is far likelier than a
    single bad row (a truncated or foreign file corrupts wholesale).

    Fixing the two that were noticed, in a change whose whole point was covering
    populations, is the failure this test exists to prevent recurring.
    """
    f = tmp_path / "cc_peer_availability.json"

    def _reads(n=4):
        for _ in range(n):
            PA.read()

    with caplog.at_level("WARNING", logger="genesis.cc.peer_availability"):
        f.write_text("{not json at all")
        _reads()
        corrupt = [r for r in caplog.records if "Corrupt" in r.getMessage()]
        assert len(corrupt) == 1, f"corrupt-file warning fired {len(corrupt)}x over 4 reads"

        caplog.clear()
        f.write_text(json.dumps({"peers": {"p" * PA._MAX_STATE_BYTES: {"available": True}}}))
        _reads()
        over = [r for r in caplog.records if "refusing to parse" in r.getMessage()]
        assert len(over) == 1, f"oversize warning fired {len(over)}x over 4 reads"


def test_a_different_bad_row_at_the_same_count_is_still_news(tmp_path, caplog):
    """Keying the suppressor on the COUNT swallowed a brand-new corruption
    whenever it replaced an old one one-for-one — while the comment above it
    claimed a file that breaks again is news a second time. Identity, not count.
    """
    f = tmp_path / "cc_peer_availability.json"
    with caplog.at_level("WARNING", logger="genesis.cc.peer_availability"):
        f.write_text(json.dumps({"peers": {"ok": _valid_row(), "bad-A": {"available": "yes"}}}))
        PA.read()
        caplog.clear()
        # Same count, different peer, different defect.
        f.write_text(json.dumps({"peers": {"ok": _valid_row(), "bad-B": {"available": 1}}}))
        PA.read()
    hits = [r for r in caplog.records if "unusable peer availability" in r.getMessage()]
    assert len(hits) == 1, "a new bad row was swallowed because the count matched"


def test_a_repaired_then_re_broken_file_warns_again(tmp_path, caplog):
    """The suppressor's docstring promises that clearing a condition RE-ARMS it —
    a file that gets fixed and then breaks again is news a second time.

    A mutation sweep caught that promise untested: deleting the re-arm entirely
    left every test green, which means the sentence was documentation of an
    intention rather than of a behaviour.

    Targeted at the row-rejection condition specifically. The file-level
    conditions key on (size, mtime), which re-arms on its own because a rewritten
    file has a new identity — so deleting the explicit re-arm is BEHAVIOURALLY
    NULL there and a test written against it proves nothing. Only a condition
    whose identity can REPEAT exactly can catch this.
    """
    f = tmp_path / "cc_peer_availability.json"
    broken = {"peers": {"ok": _valid_row(), "bad-A": {"available": "yes"}}}
    good = {"peers": {"ok": _valid_row()}}

    with caplog.at_level("WARNING", logger="genesis.cc.peer_availability"):
        f.write_text(json.dumps(broken))
        PA.read()
        assert [r for r in caplog.records if "unusable" in r.getMessage()], "no first warning"

        # Repaired: the condition no longer holds, so it must be forgotten.
        caplog.clear()
        f.write_text(json.dumps(good))
        assert set(PA.read()) == {"ok"}, "the repaired file must parse"

        # Broken again, IDENTICALLY. Same rejected row, same count — so the
        # identity alone cannot distinguish this from the first occurrence.
        caplog.clear()
        f.write_text(json.dumps(broken))
        PA.read()
    again = [r for r in caplog.records if "unusable" in r.getMessage()]
    assert len(again) == 1, f"a re-broken file must warn again, got {len(again)}"


@pytest.mark.parametrize("wording", [
    "MCP server 'web-search' returned error: 429 rate limit",
    "MCP server 'payments' returned error: usage limit reached",
    "MCP server 'payments' returned error: insufficient funds",
])
def test_an_mcp_tools_own_ceiling_is_never_the_peers_fault(wording):
    """The FALSE-POSITIVE direction, which is the expensive one here.

    `_classify_error` matches the rate-limit and quota families BEFORE its MCP
    branch, so a Genesis tool that hits its own 429 inside a peer's turn arrives
    typed CCRateLimitError — indistinguishable from a real peer refusal. The
    predicate accepted those types unconditionally, so the PEER was recorded as
    quota-blocked and that false "down" stood in every health snapshot until the
    next home-model outage.

    An earlier round measured only the MISS direction (a drained peer going
    unrecorded) and called the tradeoff settled. Measuring one side of a tradeoff
    is half a measurement, and this is the half that was missing.
    """
    from genesis.cc.invoker import CCInvoker

    exc = CCInvoker._classify_error(wording)
    assert PA.is_provider_refusal(exc) is False, (
        f"an MCP tool's own failure, typed {type(exc).__name__}, was blamed on the peer"
    )
    assert PA.note_failure("peer-x", exc) is False
    assert PA.read_peer("peer-x") is None


def test_a_genuine_peer_refusal_is_still_recorded():
    """The control. Without it the fix above is satisfiable by recording nothing."""
    from genesis.cc.invoker import CCInvoker

    exc = CCInvoker._classify_error("API error: 429 rate limit exceeded")
    assert PA.is_provider_refusal(exc) is True
    assert PA.note_failure("peer-x", exc) is True
    assert PA.read_peer("peer-x").reason == PA.QUOTA


def test_an_unreadable_state_file_is_reported_not_silently_empty(tmp_path, caplog):
    """A file that exists but cannot be read must not look like no file at all.

    `except (FileNotFoundError, OSError)` was not two cases — FileNotFoundError
    IS an OSError — so a PermissionError or EIO returned an empty map on the
    silent branch, every peer observation vanished from every health snapshot,
    and nothing said why. It also made the `except Exception` below it
    unreachable for the failure it was written to report.
    """
    f = tmp_path / "cc_peer_availability.json"
    f.write_text(json.dumps({"peers": {"ok": _valid_row()}}))
    assert set(PA.read()) == {"ok"}, "fixture must be readable first"

    f.chmod(0o000)
    try:
        with caplog.at_level("WARNING", logger="genesis.cc.peer_availability"):
            assert PA.read() == {}
        hits = [r for r in caplog.records if "Unreadable" in r.getMessage()]
        assert len(hits) == 1, f"an unreadable file must say so, got {len(hits)} warnings"
    finally:
        f.chmod(0o600)


def test_a_missing_state_file_stays_silent(tmp_path, caplog):
    """The other side of the same branch: no file yet is the NORMAL state before
    any failover has run, and must not warn on every health poll."""
    with caplog.at_level("WARNING", logger="genesis.cc.peer_availability"):
        assert PA.read() == {}
        assert PA.read() == {}
    assert not [r for r in caplog.records if "Unreadable" in r.getMessage()]


def test_a_failed_setup_does_not_leak_the_temp_descriptor(tmp_path, monkeypatch):
    """os.fchmod raising after mkstemp left the descriptor open — ownership does
    not pass to the file object until os.fdopen, and the failure path cleaned up
    the pathname only. This module writes on every failover during an outage, so
    one leak per attempt walks a long-running server into its descriptor limit.
    """
    import os as _os

    def _boom(*a, **k):
        raise OSError("filesystem refuses mode changes")

    monkeypatch.setattr(PA.os, "fchmod", _boom)

    before = len(_os.listdir("/proc/self/fd"))
    for _ in range(25):
        assert PA.note_success("peer-x") is False, "the write must report failure"
    after = len(_os.listdir("/proc/self/fd"))
    assert after - before < 5, f"leaked ~{after - before} descriptors over 25 writes"


def test_a_recurring_unreadable_file_warns_every_time_it_recurs(tmp_path, caplog):
    """The unreadable warning identifies on the file NAME, which never changes —
    so without clearing the condition on a successful read it fires once in the
    life of the process and then never again, while every health snapshot keeps
    silently losing its peers.

    A suppressor whose condition is never cleared is a suppressor that reports
    once. This is the half I left out when adding that warning.
    """
    f = tmp_path / "cc_peer_availability.json"
    f.write_text(json.dumps({"peers": {"ok": _valid_row()}}))

    def _flap():
        f.chmod(0o000)
        try:
            assert PA.read() == {}
        finally:
            f.chmod(0o600)
        assert set(PA.read()) == {"ok"}, "must recover between failures"

    with caplog.at_level("WARNING", logger="genesis.cc.peer_availability"):
        _flap()
        _flap()
        _flap()
    hits = [r for r in caplog.records if "Unreadable" in r.getMessage()]
    assert len(hits) == 3, f"three separate outages must warn three times, got {len(hits)}"


def test_deeply_nested_json_does_not_disable_recording_forever(tmp_path):
    """A syntactically VALID file can still break the parser: ~20KB of nested
    arrays exceeds the recursion limit, and RecursionError is not a ValueError,
    so it escaped the corrupt-file handler entirely.

    `read()` absorbs that in its outer guard, which is why this hid — but the
    WRITE path calls `_read_raw` directly, so every record attempt failed before
    reaching `_write` and the file could never be overwritten. Recording stayed
    disabled until a human deleted the file. A corrupt file must always be
    recoverable by writing over it; that is the whole reason this path returns
    empty rather than raising.
    """
    f = tmp_path / "cc_peer_availability.json"
    f.write_text("[" * 10_000 + "]" * 10_000)
    assert f.stat().st_size < PA._MAX_STATE_BYTES, "must be under the size cap to isolate this"

    assert PA.read() == {}, "read() must survive it"
    # The real property: the write path SELF-HEALS rather than being wedged.
    assert PA.note_failure("peer-x", CCRateLimitError("429")) is True
    assert PA.read_peer("peer-x") is not None, "the file never recovered"


def test_an_older_observation_never_overwrites_a_newer_one(tmp_path):
    """The stamp is taken BEFORE the lock, and the lock's bounded retry means two
    concurrent recorders can win it in either order — so a delayed failure could
    replace a fresher success, and the reversal stands until the next outage.

    Simulated by writing the rows in reversed stamp order through the real merge
    path, which is exactly what the losing interleaving produces.
    """
    from datetime import timedelta

    newer = datetime.now(UTC)
    older = newer - timedelta(seconds=30)

    def _status(available, stamp):
        return PA.PeerStatus(
            peer="peer-x", available=available,
            reason="" if available else PA.QUOTA,
            observed_at=stamp.isoformat(), reset_at="",
            limit_kind="" if available else "unknown",
        )

    # _merge_and_write owns the ordering rule (its caller holds the lock; no
    # concurrency needed to exercise the losing interleaving's exact writes).
    assert PA._merge_and_write("peer-x", _status(True, newer)) is True
    assert PA._merge_and_write("peer-x", _status(False, older)) is True
    st = PA.read_peer("peer-x")
    assert st is not None and st.available is True, (
        "a 30s-older failure overwrote a fresh success"
    )
    # And the newer-wins rule is not sticky-first: a genuinely newer write lands.
    assert PA._merge_and_write(
        "peer-x", _status(False, newer + timedelta(seconds=5))
    ) is True
    assert PA.read_peer("peer-x").available is False


def test_a_future_stamp_cannot_pin_the_record_forever(tmp_path):
    """The newer-wins merge compares blindly, so a single future-stamped row —
    a foreign file, or a clock corrected backward after a write — would out-rank
    every genuine observation FOREVER while _merge_and_write kept returning
    True. Recording would look healthy and write nothing.

    The decoder now rejects a stamp beyond a small skew horizon, so the poisoned
    row drops on read and the next real observation lands.
    """
    (tmp_path / "cc_peer_availability.json").write_text(json.dumps({"peers": {
        "peer-x": {
            "available": False, "reason": "quota",
            "observed_at": "9999-12-31T23:59:59+00:00",
            "reset_at": "", "limit_kind": "unknown",
        },
    }}))
    assert PA.read() == {}, "a future-stamped row must be rejected, not trusted"
    assert PA.note_success("peer-x") is True
    st = PA.read_peer("peer-x")
    assert st is not None and st.available is True, "the poisoned row pinned the record"


def test_a_small_clock_skew_does_not_reject_an_honest_row():
    """The other direction: five minutes of tolerance means an NTP step cannot
    silently discard real observations."""
    from datetime import timedelta

    near_future = (datetime.now(UTC) + timedelta(seconds=60)).isoformat()
    (PA._state_path()).write_text(json.dumps({"peers": {
        "peer-x": {
            "available": True, "reason": "",
            "observed_at": near_future, "reset_at": "", "limit_kind": "",
        },
    }}))
    assert set(PA.read()) == {"peer-x"}, "a 60s skew must not reject the row"


def test_fdopen_failure_does_not_leak_the_descriptor(monkeypatch):
    """Round-8's fix cleared `fd` BEFORE os.fdopen returned, so an fdopen raise
    left the descriptor open with the failure path seeing None — the exact
    one-leak-per-write the fix claimed to close. Ownership now transfers only
    after fdopen succeeds."""
    import os as _os

    def _boom(*a, **k):
        raise MemoryError("allocation pressure")

    monkeypatch.setattr(PA.os, "fdopen", _boom)
    before = len(_os.listdir("/proc/self/fd"))
    for _ in range(25):
        assert PA.note_success("peer-x") is False
    after = len(_os.listdir("/proc/self/fd"))
    assert after - before < 5, f"leaked ~{after - before} descriptors over 25 writes"


def test_mcp_evidence_is_seen_even_when_split_across_streams():
    """The classifier MATCHES on combined stderr+stdout but constructs the typed
    exception from stderr alone, keeping the combined text in raw_text. So an
    MCP marker on stdout with the limit text on stderr was invisible to a
    predicate reading str(exc) — and the peer was wrongly marked quota-blocked.
    Built through the REAL classifier (supply, not just the predicate)."""
    from genesis.cc.invoker import CCInvoker

    exc = CCInvoker._classify_error(
        "API error: 429 rate limit",                       # stderr → str(exc)
        "MCP server 'web-search' request log",             # stdout → raw_text only
    )
    assert "mcp" not in str(exc).lower(), "premise: the marker is NOT in str(exc)"
    assert PA.is_provider_refusal(exc) is False, (
        "split-stream MCP evidence was missed and the peer blamed"
    )


def test_transient_lock_errors_fail_closed_not_open(tmp_path, monkeypatch):
    """EINTR is retried; a genuinely-unsupported flock degrades open; everything
    else (ENOLCK, EIO, …) fails CLOSED — flock EXISTS there, so another process
    may hold it and an unserialized write can clobber its freshly written map,
    which is the measured data-loss shape this lock was built against."""
    import errno as _errno

    calls = {"n": 0}
    real_flock = PA.fcntl.flock

    def _eintr_then_ok(fd, op):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(_errno.EINTR, "interrupted")
        return real_flock(fd, op)

    monkeypatch.setattr(PA.fcntl, "flock", _eintr_then_ok)
    assert PA.note_success("peer-x") is True, "EINTR must be retried, not fatal"

    def _enolck(fd, op):
        raise OSError(_errno.ENOLCK, "no locks available")

    monkeypatch.setattr(PA.fcntl, "flock", _enolck)
    assert PA.note_success("peer-y") is False, "ENOLCK must fail closed"
    assert PA.read_peer("peer-y") is None, "a closed failure must not have written"

    def _enotsup(fd, op):
        raise OSError(_errno.ENOTSUP, "not supported")

    monkeypatch.setattr(PA.fcntl, "flock", _enotsup)
    assert PA.note_success("peer-z") is True, "unsupported flock degrades open"
    assert PA.read_peer("peer-z") is not None


def test_an_unopenable_lock_file_fails_closed(tmp_path, monkeypatch):
    """Failing to OPEN the lock file is not proof no lock exists — another
    process with permissions may hold it right now. The old branch degraded
    open and could clobber that holder's map."""
    real_open = PA.Path.open

    def _fail_lock_open(self, *a, **k):
        if self.name == PA._LOCK_FILE:
            raise PermissionError("lock file unreadable")
        return real_open(self, *a, **k)

    monkeypatch.setattr(PA.Path, "open", _fail_lock_open)
    assert PA.note_success("peer-x") is False
