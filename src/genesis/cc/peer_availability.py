"""Last-known availability of each CC roster failover peer (cross-process).

WHY THIS EXISTS. ``roster.failover_chain`` admits a peer on CREDENTIAL PRESENCE
— "is ``auth_env`` set" — never on whether the peer can actually serve. So a peer
whose provider quota is exhausted looks identical to a healthy backup everywhere
Genesis reports on itself: the health snapshot shows the home model fine and says
nothing about the standby being unusable. This module records what the last real
attempt actually showed, so the difference is visible.

WHAT IT RECORDS, PRECISELY: a PROVIDER REFUSAL. Only a rate-limit / quota error
says anything about the peer. A local fault — no network, our own timeout, a
Genesis MCP server crash, a stale session id — is also a ``CCError`` on the same
code path but reaches it having received no answer from the provider (in the
offline case, without a packet leaving the box). Attributing those to the peer
would mark the whole standby fleet down for a local blip, publishing exactly the
confident-but-wrong picture this module exists to prevent. ``note_failure`` owns
that classification so the policy lives in ONE testable place.

ADVISORY ONLY — it MUST NOT gate failover. The failover path tries every peer in
order regardless of what is recorded here. A stale or wrong "unavailable" record
that suppressed a peer would remove a WORKING backup at exactly the moment the
home model is down, turning a recoverable outage into a degraded one. Observation
is cheap to get wrong; suppression is not.

NOT A PROBE. Nothing here calls a provider. Records are a side effect of failover
attempts that were happening anyway, so observation costs zero quota. An active
preflight would spend the very budget it protects — on a rolling usage window the
check itself can cause the exhaustion it reports.

STALENESS IS THE HONEST CAVEAT. A record is only refreshed when a failover
actually runs, which happens only while the HOME model is down. So a peer whose
quota resets an hour later is not re-observed, and this file keeps reporting the
last thing that was true — possibly for days. That is why every record carries
``observed_at``, why readers get ``age_seconds``, and why the fields are named
"last observed" rather than "is". Nothing here should be read as current state.
Closing that gap needs a peer-recovery probe, which is deliberately not this
module (see the module docstring of ``cc_fallback_probe`` for the home-model
equivalent).

Written with the same discipline as ``fallback_state.py``: atomic replace so a
reader never sees partial JSON, and never raises (it sits on the conversation
failover path).

NO FREE TEXT IS EVER STORED, and that is a structural guarantee rather than a
careful one. Every field is a closed set — a bool, one of two reason values, one
of four limit kinds, or a timestamp that must round-trip byte-identically. There
is no field a provider's prose could occupy, so there is nothing to scrub, no
credentials to discover, and no way for this record to carry a secret.

That is a DELETION, not a hardening, and it was the answer to four review rounds.
An earlier design stored the provider's refusal text and needed ~150 lines to
make it safe: redaction by env-var name, the roster's user-chosen ``auth_env``
names, and a fail-closed guard for "did that roster read actually succeed?".
Every attempt to answer that last question had a hole, because Genesis config
loaders degrade silently BY DESIGN — so no downstream inference could be sound.
The prose now goes to the log at the point of failure (see
``conversation._try_roster_failover``), which is both a better home for it and
outside the exposure path below.

THE EXPOSURE PATH, for anyone tempted to add a text field back: this file is read
into every health snapshot, reaches an LLM's context through the health MCP tool
(``mcp/health/status.py``), and is JSON-dumped whole into
``sentinel/monitor.py``'s monitoring prompt with no sanitisation. It is NOT
captured by backups (``scripts/backup.sh`` is selective), but the first three are
enough. A free-text field here is a free-text field in an LLM prompt.

NOTHING IS EVER TRUNCATED OR REPAIRED. A row read from disk either matches
exactly what this module writes or it is DROPPED (see ``_decode_row``). Repairing
a foreign value accepts by default, and every unanticipated value is then a hole
someone finds later; rejecting by default makes the accept-set exactly the
vocabulary we emit.

CONCURRENCY: the read-modify-write is SERIALIZED across processes by a sidecar
flock (see ``_record_lock``). An earlier draft accepted last-writer-wins on the
grounds that the record was advisory and self-healing — but "self-healing" was
itself retracted (records refresh only during a home-model outage), so a dropped
observation can stand for days, which is the exact silence this module exists to
remove. The lock is load-bearing: do not remove it. It is non-blocking with a
bounded retry and degrades OPEN, so it cannot stall the event loop.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import logging
import os
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from genesis.env import genesis_home

logger = logging.getLogger(__name__)

_STATE_FILE = "cc_peer_availability.json"

#: Sanity bound on a peer NAME. NOT a truncation bound — a longer name is
#: DROPPED, never cut, because cutting two names to a shared prefix merges two
#: peers onto one key and lets one peer's success clear another's failure.
#: `roster.py` applies no length bound of its own (zero `len()` calls), so this
#: is the only backstop, and it is set far above any plausible roster name.
_MAX_PEER_NAME = 512

#: Refuse to PARSE a state file larger than this. `read()` runs synchronously on
#: the health path; bounding the row count after json.loads() still pays the
#: whole memory spike first, on a box shared with other sessions. 32 peers of
#: bounded fields cannot approach this, so exceeding it means the file is not
#: ours to interpret.
_MAX_STATE_BYTES = 1_000_000

#: An ISO8601 stamp with offset is ~32 chars. This is a REJECT bound, never a
#: truncation: a longer value is not a stamp we wrote, so its row is dropped.
#: It exists so a pathological value is refused before `fromisoformat` parses it.
_MAX_STAMP = 64

#: Hard cap on tracked peers. A roster holds a handful; this only bounds a
#: pathological case (a renamed/rotating peer id) so the file cannot grow without
#: limit — it is read whole into every health snapshot.
_MAX_PEERS = 32

#: The only reason recorded. A named constant so the recorder, the snapshot and
#: any future renderer agree on the string. No dashboard template consumes it
#: today — verified, not assumed.
QUOTA = "quota"

#: Closed sets for the two enum-valued fields. ``_LIMIT_KINDS`` is DERIVED from
#: the parser that produces the value rather than re-typed here, so the two
#: cannot drift apart: a kind added there is accepted here without an edit.
_REASONS = frozenset({"", QUOTA})


def _load_limit_kinds() -> frozenset[str]:
    # Resolved ONCE at import, not per row. A deferred import inside the decoder
    # made read() capable of raising ImportError, contradicting its documented
    # "Never raises" contract on the health snapshot path.
    try:
        from genesis.cc.rate_limit_reset import SESSION, UNKNOWN, WEEKLY

        return frozenset({"", SESSION, WEEKLY, UNKNOWN})
    except Exception:  # pragma: no cover — the module is a sibling with no deps
        logger.warning("rate_limit_reset unavailable — limit_kind will not validate")
        return frozenset({""})


_LIMIT_KINDS = _load_limit_kinds()


def _state_path() -> Path:
    return genesis_home() / _STATE_FILE


@dataclass(frozen=True)
class PeerStatus:
    """What the LAST OBSERVED attempt against a peer showed.

    Not current state — see the module docstring on staleness. ``observed_at`` is
    the only thing that makes this record interpretable.
    """

    peer: str = ""
    available: bool = True
    reason: str = ""  # "" when available, else QUOTA
    observed_at: str = ""  # ISO8601 UTC of the observation
    reset_at: str = ""  # ISO8601 when the provider's reset time was parseable
    limit_kind: str = ""  # session / weekly / unknown, per rate_limit_reset


def _read_raw() -> dict:
    """Raw peer map from disk. Never raises — any unreadable file reads empty."""
    path = _state_path()
    try:
        # Bound the INPUT, not just the parsed result: this runs synchronously on
        # the health path, and discarding rows after json.loads() has already
        # paid the whole memory spike. Anything this large is not a file we wrote.
        size = path.stat().st_size
        if size > _MAX_STATE_BYTES:
            logger.warning(
                "%s is %d bytes (cap %d) — refusing to parse it",
                _STATE_FILE, size, _MAX_STATE_BYTES,
            )
            return {}
        raw = path.read_text(encoding="utf-8", errors="replace")
    except (FileNotFoundError, OSError):
        return {}
    except Exception:  # pragma: no cover — defence in depth on a hot path
        logger.warning("Unreadable %s — treating as empty", _STATE_FILE, exc_info=True)
        return {}
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        logger.warning("Corrupt %s — treating as empty", _STATE_FILE, exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    peers = data.get("peers")
    return peers if isinstance(peers, dict) else {}


def _decode_stamp(value: object) -> str | None:
    """The stamp we EMIT, or None to reject the row. ``""`` is a valid absent stamp.

    Round-tripping is the whole check, and it is what makes this closed-set:
    a value is accepted only if ``fromisoformat(v).isoformat() == v``, i.e. only
    if it is byte-identical to something this module would have written. An ISO
    string with an arbitrarily long fractional-second component parses fine and
    normalises to microseconds, so it fails the round trip and its row is
    dropped — where a "validate it parses" check accepted it and returned the
    original unbounded string straight into a health snapshot.
    """
    if value == "":
        return ""
    if not isinstance(value, str) or len(value) > _MAX_STAMP:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    # The round trip alone is TZ-AGNOSTIC: a naive stamp round-trips perfectly
    # and was accepted, though `_record` only ever writes `datetime.now(UTC)`.
    # The snapshot then subtracts an aware `now` from it, raises TypeError, and
    # reports `age_seconds: null` — so a blocked peer arrives in an LLM's context
    # with no staleness at all, which is the one thing that makes this record
    # interpretable. Rejecting is right: naive is not something we emit.
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    if parsed.isoformat() != value:
        return None
    return value


def _decode_row(peer: object, raw: object) -> tuple[str, dict] | None:
    """STRICTLY decode one (peer, row) pair, or None to DROP the row.

    THE mechanism this module's read path is built on, and the one that replaced
    a per-field REPAIR layer. The difference is the default, and it is the whole
    design: repair ACCEPTS by default and every unanticipated value is a hole to
    be found later; this REJECTS by default, so the accept-set is exactly the
    vocabulary we emit and a value nobody thought of is dropped rather than
    coerced into a health snapshot.

    Four review rounds found seven holes in the repair layer — naive-vs-aware
    stamps, unbounded fractional seconds, ``bool("false") is True``, a truncated
    name merging two peers, an unbounded row count, an unbounded file, an
    unscrubbed re-publish. Every one was an accept that should have been a
    reject. None of them is reachable here, because nothing is repaired: a row
    either matches what we write, exactly, or it is not a row.

    Rejection is per ROW, never per document — one corrupt row must not destroy
    every other peer's observation. The dropped row self-heals on the next write.
    """
    if not isinstance(peer, str) or not peer or len(peer) > _MAX_PEER_NAME:
        return None
    if not isinstance(raw, dict):
        return None

    # A real JSON boolean — NOT Python truthiness. `bool(raw.get("available"))`
    # read the string "false" as True and a missing key as an invented healthy
    # observation, both of which are surfaced to consumers as fact.
    available = raw.get("available")
    if not isinstance(available, bool):
        return None

    reason = raw.get("reason", "")
    limit_kind = raw.get("limit_kind", "")
    if not isinstance(reason, str) or reason not in _REASONS:
        return None
    if not isinstance(limit_kind, str) or limit_kind not in _LIMIT_KINDS:
        return None

    observed_at = _decode_stamp(raw.get("observed_at", ""))
    reset_at = _decode_stamp(raw.get("reset_at", ""))
    if observed_at is None or reset_at is None:
        return None
    # We ALWAYS write an observation time, and a record without one is
    # uninterpretable — the whole module turns on "how stale is this?".
    if not observed_at:
        return None

    # Coherence, because "what we emit" includes the RELATIONSHIP between fields:
    # an available peer carries no refusal detail, and an unavailable one always
    # carries the one reason this module records.
    if available and (reason or reset_at or limit_kind):
        return None
    if not available and reason != QUOTA:
        return None

    return peer, {
        "available": available,
        "reason": reason,
        "observed_at": observed_at,
        "reset_at": reset_at,
        "limit_kind": limit_kind,
    }


def read() -> dict[str, PeerStatus]:
    """Every recorded peer's last-observed status. Never raises.

    Detail is re-bounded on read so a file written by an older/other build cannot
    push an oversized blob into a health snapshot (and from there into an LLM
    context via the health MCP tool).
    """
    out: dict[str, PeerStatus] = {}
    dropped = 0
    try:
        for name, raw in _read_raw().items():
            pair = _decode_row(name, raw)
            if pair is None:
                dropped += 1
                continue
            key, row = pair
            out[key] = PeerStatus(peer=key, **row)
    except Exception:
        # "Never raises" is a CONTRACT, not an aspiration — this runs on the
        # health snapshot path and an earlier revision could raise ImportError
        # through a deferred import. A partial map beats an exception here.
        logger.warning("peer availability read failed — returning what parsed", exc_info=True)
    if dropped:
        # ONE line per read, not one per row: this is on the health path and a
        # single malformed file would otherwise re-log on every snapshot forever.
        logger.warning("ignored %d unusable peer availability row(s)", dropped)
    if len(out) > _MAX_PEERS:
        # The cap must hold HERE, not only on write. This file is copied whole
        # into every health snapshot and from there into an LLM context, while
        # write-side eviction only runs when an observation is recorded — which
        # happens only during a home-model outage. A foreign or older file would
        # otherwise inflate every snapshot for days.
        dropped = len(out) - _MAX_PEERS
        logger.warning("state file holds %d peers (cap %d) — ignoring %d",
                       len(out), _MAX_PEERS, dropped)
        out = dict(list(out.items())[-_MAX_PEERS:])
    return out


def read_peer(peer: str) -> PeerStatus | None:
    """One peer's last-observed status, or None if never observed."""
    return read().get(peer)


#: How a third-party Anthropic-compatible endpoint refuses a DRAINED prepaid
#: account. Matched here, on the exception text, rather than added to the
#: invoker's global ``_QUOTA_PATTERNS`` — and that placement is the whole point.
#: The global classifier decides CC STATUS and PARKING: a CCQuotaExhaustedError
#: relays to an account-wide ``CCStatus.UNAVAILABLE`` with no notion of whether
#: the invocation was the home model or a failover peer, so teaching it these
#: phrases would let a drained BACKUP report the primary as down, and would also
#: outrank the invoker's later MCP classification (an MCP tool whose own error
#: text says "insufficient funds" would be parked as a provider quota failure).
#: This module is advisory-only, so the same knowledge is safe here and affects
#: nothing but the record.
_BALANCE_REFUSALS = (
    "insufficient balance",
    "insufficient credit",
    "insufficient funds",
    "insufficient_quota",
)


def is_provider_refusal(exc: BaseException) -> bool:
    """Public alias — the single place that decides what counts as evidence.

    Callers need this SEPARATELY from ``note_failure``'s return value, which is
    False for four different reasons (declined classification, lock contention,
    a failed write, an internal fault). Treating "returned False" as "declined"
    let a transient write failure convert a provider refusal into a recorded
    SUCCESS — the opposite observation, standing for days.
    """
    return _is_provider_refusal(exc)


def _is_provider_refusal(exc: BaseException) -> bool:
    """True only for an error the PROVIDER returned about capacity or credit.

    Deliberately narrow. Every other ``CCError`` on the failover path is
    ambiguous about the peer (see the module docstring), and a wrong "down" is
    worse than no record.
    """
    from genesis.cc.exceptions import CCQuotaExhaustedError, CCRateLimitError

    if isinstance(exc, (CCRateLimitError, CCQuotaExhaustedError)):
        return True
    # A drained account is a refusal in substance but reaches us as a generic
    # process error, because the global classifier deliberately does not know
    # these phrases (see above). Text-matching is confined to this advisory path.
    from genesis.cc.exceptions import CCProcessError

    if isinstance(exc, CCProcessError):
        blob = str(exc).lower()
        return any(p in blob for p in _BALANCE_REFUSALS)
    return False


def note_failure(peer: str, exc: BaseException) -> bool:
    """Record a failed attempt IFF the failure is attributable to the provider.

    Returns True when a record was written. Callers may pass ANY failover
    exception — the classification lives here on purpose, so there is exactly one
    place that decides what counts as evidence about a peer. Never raises.

    Synchronous (roster read, lock, tempfile, fsync, replace) — call via
    ``asyncio.to_thread`` from async code; it sits on the async failover path.
    """
    try:
        if not peer or not _is_provider_refusal(exc):
            return False
    except Exception:
        # This module promises it never raises, and it sits on the failover
        # path: a raise here escapes into the peer loop and abandons every
        # REMAINING peer, turning an observability helper into an outage
        # amplifier. _is_provider_refusal imports and str()s a foreign
        # exception, both of which can raise on a hostile __str__.
        logger.debug("peer availability classification failed for %s", peer, exc_info=True)
        return False
    reset_at, limit_kind = "", ""
    try:
        from genesis.cc.rate_limit_reset import parse_reset

        # Reuse the existing parser rather than re-deriving reset semantics; it
        # already owns the "ambiguous hint -> return None" policy, and returning
        # None here simply leaves reset_at empty.
        kind, when = parse_reset(
            raw_event=getattr(exc, "raw_event", None),
            raw_text=getattr(exc, "raw_text", None),
            now=datetime.now(UTC),
        )
        limit_kind = kind or ""
        reset_at = when.isoformat() if when is not None else ""
    except Exception:
        logger.debug("reset parse failed for peer %s", peer, exc_info=True)
    return _record(
        peer,
        available=False,
        reason=QUOTA,
        reset_at=reset_at,
        limit_kind=limit_kind,
    )


def note_success(peer: str) -> bool:
    """Record that the peer served a turn, clearing any stale block. Never raises.

    Synchronous — call via ``asyncio.to_thread`` from async code.
    """
    return _record(peer, available=True)


#: Sidecar lock for the read-modify-write in :func:`_record`. A SEPARATE file on
#: purpose: the state file is published with ``os.replace``, so a lock held on
#: its inode would be released the moment a writer swapped it — the lock has to
#: outlive the file it protects.
_LOCK_FILE = "cc_peer_availability.lock"

#: Lock acquisition is NON-BLOCKING with a bounded retry.
#:
#: The budget used to be 10 x 20ms because this ran synchronously inside an async
#: failover turn, where a blocking flock would stall the event loop. THAT
#: CONSTRAINT IS GONE — recording now runs via ``asyncio.to_thread``
#: (``conversation._record_peer``), so the wait costs a worker thread, not the
#: loop. The old budget survived the move and was far too tight: acquisition is a
#: LOTTERY, not a queue, so with 12 contending writers an unlucky one could lose
#: all 10 draws. MEASURED at that budget: ~50% of runs lost at least one row.
#:
#: Still bounded rather than a blocking LOCK_EX, so a wedged holder cannot pin a
#: worker thread indefinitely. 40 x 25ms = 1s: five times the measured need
#: (loss appeared at a 0.2s budget) and short enough that a stuck holder cannot
#: monopolise one of asyncio's ~10 shared default-executor workers for long.
_LOCK_ATTEMPTS = 40
_LOCK_SLEEP_S = 0.025

#: errnos that mean "someone else holds it" as opposed to "this filesystem has no
#: flock". The distinction decides the FAIL DIRECTION, so it is not cosmetic:
#: contention must fail CLOSED (see below), a missing implementation may not.
_LOCK_CONTENDED = frozenset({errno.EACCES, errno.EAGAIN, errno.EWOULDBLOCK})


@contextlib.contextmanager
def _record_lock():
    """Serialize the read-modify-write across processes. Never raises.

    Yields True when the caller may write, False when it must not.

    Without it the sequence is last-writer-wins: two processes recording
    DIFFERENT peers in the same instant each read the map, each add their row,
    and the second write drops the first. A lost record can stand for days
    (records refresh only during a home-model outage), which is exactly the
    silence this module exists to remove.

    THE FAIL DIRECTION IS SPLIT, and getting it wrong is what caused a measured
    data-loss bug rather than merely risking one:

    * **Contention** (someone holds the lock) fails CLOSED — yield False, write
      nothing. An earlier version "degraded open" here and wrote unserialized,
      which does not rescue our row; it CLOBBERS the holder's. Losing our own
      observation and reporting False is strictly better than silently
      destroying someone else's, and ``_record`` already promises that True
      means the row landed.
    * **No flock on this filesystem** degrades OPEN — yield True and write
      unserialized. There is no lock to wait for, so refusing would disable
      recording entirely on that platform. A racy record beats no record.
    """
    fh, acquired, may_write = _acquire_record_lock()
    try:
        yield may_write
    finally:
        if fh is not None and acquired:
            with contextlib.suppress(Exception):
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        if fh is not None:
            with contextlib.suppress(Exception):
                fh.close()


def _acquire_record_lock():
    """Try to take the lock. Returns ``(fh, acquired, may_write)``. Never raises.

    Split out of the context manager on purpose: with the acquisition loop inline,
    the ``yield False`` for the contended case sat INSIDE the outer
    ``try/except Exception``. An exception thrown in at that yield was swallowed,
    control fell through to the second yield, and contextlib raised
    ``RuntimeError("generator didn't stop after throw()")`` — replacing the real
    error with a misleading one. The generator now has exactly one yield and no
    enclosing except, so it cannot eat a caller's exception.
    """
    path = _state_path().parent / _LOCK_FILE
    fh = None
    acquired = False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = path.open("a+")
        for attempt in range(_LOCK_ATTEMPTS):
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except OSError as exc:
                if exc.errno not in _LOCK_CONTENDED:
                    # Not contention — flock is unavailable here. Degrade open.
                    logger.debug("flock unsupported — proceeding unserialized", exc_info=True)
                    with contextlib.suppress(Exception):
                        fh.close()
                    fh = None
                    break
                if attempt == _LOCK_ATTEMPTS - 1:
                    logger.warning(
                        "peer availability lock held for %.1fs — not recording this "
                        "observation rather than clobbering the holder's",
                        _LOCK_ATTEMPTS * _LOCK_SLEEP_S,
                    )
                    with contextlib.suppress(Exception):
                        fh.close()
                    fh = None
                    return None, False, False
                time.sleep(_LOCK_SLEEP_S)
    except Exception:
        # Could not even open the lock file. Same reasoning as "no flock".
        logger.debug("peer availability lock unavailable — proceeding unserialized", exc_info=True)
        if fh is not None:
            with contextlib.suppress(Exception):
                fh.close()
            fh = None
    return fh, acquired, True


def _record(
    peer: str,
    *,
    available: bool,
    reason: str = "",
    reset_at: str = "",
    limit_kind: str = "",
) -> bool:
    if not peer:
        return False
    try:
        status = PeerStatus(
            peer=peer,
            available=available,
            reason="" if available else reason,
            observed_at=datetime.now(UTC).isoformat(),
            reset_at="" if available else reset_at,
            limit_kind="" if available else limit_kind,
        )
        # Merge onto a DECODED view of what is on disk, under the lock so a
        # concurrent recorder cannot drop this row. Writing _read_raw() straight
        # back would re-persist whatever another build left there — the decode is
        # what stops a foreign row surviving a merge it never passed.
        with _record_lock() as may_write:
            if not may_write:
                return False
            return _merge_and_write(peer, status)
    except Exception:
        logger.debug("peer availability record failed for %s", peer, exc_info=True)
        return False


def _merge_and_write(peer: str, status: PeerStatus) -> bool:
    """Read-modify-write the peer map. Caller holds :func:`_record_lock`.

    Merges onto a DECODED view of what is on disk, so a row that would not pass
    ``read()`` cannot survive by being merged forward either. Reader and writer
    share one decoder precisely so they cannot disagree about what a row is.
    """
    peers = {}
    for k, v in _read_raw().items():
        pair = _decode_row(k, v)
        if pair is not None:
            peers[pair[0]] = pair[1]
    pair = _decode_row(peer, {k: v for k, v in asdict(status).items() if k != "peer"})
    if pair is None:
        # An unstorable name is not recordable, and saying so is the honest
        # return: _record's contract is that True means the row landed.
        return False
    peer, row = pair
    # pop-then-set UNCONDITIONALLY: assigning an existing key does NOT move it in
    # a dict, so without this the order is first-seen and eviction below drops the
    # MOST RECENTLY observed peer while keeping stale ones. Measured: re-recording
    # the freshest peer then adding one id evicted the fresh peer.
    peers.pop(peer, None)
    peers[peer] = row
    if len(peers) > _MAX_PEERS:
        # Evict by INSERTION ORDER, not by timestamp. Ranking on observed_at
        # produced three review findings across two rounds — a raw-string sort
        # that ranked "zzzz" above every ISO stamp, then a parsed sort that
        # raised TypeError the moment a legacy naive stamp met an aware one —
        # and each failed the same silent way: the sort blew up inside the
        # guarded block, _record returned False, and recording stopped for good.
        # A ranking that must parse legacy-shaped data, on a path whose whole
        # purpose is honest bookkeeping, is the wrong mechanism; it is gone
        # rather than fixed a third time. dicts preserve insertion order, the
        # row written this call is re-inserted last, and nothing here can raise.
        # The row written this call is already last (pop-then-set above), so
        # keeping the tail keeps it plus the most recently observed others.
        peers = dict(list(peers.items())[-_MAX_PEERS:])
    return _write({"peers": peers})


def _write(payload: dict) -> bool:
    """Atomic write (temp file + os.replace). Returns True iff the file landed.

    The return value is load-bearing, not decoration: a caller that reports a
    recorded observation while the write failed is exactly the confident-but-
    wrong signal this module exists to remove. A failure between mkstemp and
    os.replace also leaves the temp file behind, so it is unlinked here —
    otherwise a disk-full condition accumulates orphaned .tmp files next to the
    state it could not write.
    """
    path = _state_path()
    tmp: str | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        # os.fdopen, not a bare os.write: os.write may legally write FEWER bytes
        # than asked without raising (disk pressure, file-size limits), and this
        # code then fsynced and atomically replaced a valid state file with
        # truncated JSON while reporting success. A file object loops until the
        # payload is written or raises trying, so a short write can no longer be
        # mistaken for a complete one.
        # EXPLICIT, not incidental. mkstemp already creates 0600 and os.replace
        # preserves it, but this file can hold provider prose, so the mode is
        # load-bearing and should not depend on a default staying put.
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(json.dumps(payload, indent=2).encode())
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, str(path))
        return True
    except Exception:
        # Exception, not OSError: json.dumps can raise TypeError on an
        # unserialisable value, which left the temp file behind while the
        # docstring promised cleanup.
        logger.error("Failed to write %s", _STATE_FILE, exc_info=True)
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        return False
