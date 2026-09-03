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
failover path). No secret is stored — provider text is scrubbed BEFORE it is
truncated, because truncating first lets a secret straddling the bound survive as
an unmatched prefix.

CONCURRENCY: last-writer-wins. Two processes recording different peers in the
same instant can lose one update. Accepted deliberately — the record is advisory.
Do not add locking without a measured reason; this is on a degraded-path loop.
"""

from __future__ import annotations

import contextlib
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

#: Bound on stored provider prose. Enough to identify the failure; short enough
#: that a pathological error body cannot bloat the file. Applied AFTER scrubbing.
_MAX_TEXT = 300

#: Hard cap on a peer NAME. A roster name is short; anything longer is foreign
#: data, and an unbounded key is as effective a bloat vector as an unbounded
#: value — both end up in the health snapshot and, through the health MCP tool,
#: in an LLM's context.
_MAX_PEER_NAME = 128

#: Hard cap on tracked peers. A roster holds a handful; this only bounds a
#: pathological case (a renamed/rotating peer id) so the file cannot grow without
#: limit — it is read whole into every health snapshot.
_MAX_PEERS = 32

#: Only environment variables whose NAME looks credential-bearing are scrubbed.
#: Filtering by value length alone also redacts PWD / VIRTUAL_ENV /
#: ANTHROPIC_BASE_URL, which destroys the only human-readable field in the record
#: — a provider connection error would render as "<redacted>".
_SECRET_NAME_HINTS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL")

#: Below this length a value is too generic to redact safely (an env var set to
#: "1" would blank every digit in the prose while protecting nothing).
_MIN_SECRET_LEN = 8

#: The only reason recorded. Kept as a named constant so call sites and the
#: dashboard agree on the string.
QUOTA = "quota"


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
    detail: str = ""  # bounded, scrubbed provider text
    reset_at: str = ""  # ISO8601 when the provider's reset time was parseable
    limit_kind: str = ""  # session / weekly / unknown, per rate_limit_reset


def _secret_names() -> set[str]:
    """Environment variable names to treat as credential-bearing.

    Name hints, PLUS every ``auth_env`` the roster itself declares. Those names
    are user-chosen (``auth_env: GLM_PAT``) and need not contain any hint word,
    so hint-matching alone would write a peer's own token to disk verbatim —
    precisely the credential most likely to appear in that peer's error text.
    """
    names = {n for n in os.environ if any(h in n.upper() for h in _SECRET_NAME_HINTS)}
    try:
        from genesis.cc import roster

        for raw in (roster.load_roster().get("models") or {}).values():
            if isinstance(raw, dict) and raw.get("auth_env"):
                names.add(str(raw["auth_env"]))
    except Exception:
        # Never let roster resolution break a scrub — hints still apply.
        logger.debug("roster auth_env lookup failed during scrub", exc_info=True)
    return names


def _secret_values() -> list[str]:
    """Credential values worth redacting, LONGEST FIRST.

    Order matters: when one secret contains another (a token and a prefix of it
    both set in the environment), redacting the shorter first would leave the
    longer one's tail behind as an unmatched fragment.
    """
    out = []
    for name in _secret_names():
        value = os.environ.get(name)
        if value and len(value) >= _MIN_SECRET_LEN:
            out.append(value)
    return sorted(out, key=len, reverse=True)


def _scrub(text: str) -> str:
    """Redact credential values, THEN bound the length.

    Order is load-bearing and was a real defect: truncating first lets a secret
    that straddles the bound lose its tail, so it no longer matches the
    environment value and its head is written to disk verbatim. Reproduced with a
    64-char token at offset 290 before this was fixed. This file is persisted and
    lands in backups, so a partial-prefix leak is a real leak.
    """
    out = (text or "").strip()
    if not out:
        return ""
    for value in _secret_values():
        if value in out:
            out = out.replace(value, "<redacted>")
    return out[:_MAX_TEXT]


def _read_raw() -> dict:
    """Raw peer map from disk. Never raises — any unreadable file reads empty."""
    try:
        raw = _state_path().read_text(encoding="utf-8", errors="replace")
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


def _sanitise(peer: str, raw: dict) -> tuple[str, dict]:
    """Bound a (peer, row) pair. THE single place width is enforced.

    Previously only ``detail`` was bounded, and only on the way in — so every
    other field of a row written by another build was re-persisted forever and
    handed to the snapshot untouched. MEASURED before this existed: one foreign
    row produced a 300KB state file and a 100,000-character ``reason`` in a
    single snapshot row. Reader and writer both go through here so the two can
    never disagree about what "bounded" means.
    """
    name = str(peer)[:_MAX_PEER_NAME]
    row = {
        "available": bool(raw.get("available", True)),
        "reason": str(raw.get("reason", ""))[:_MAX_TEXT],
        "observed_at": str(raw.get("observed_at", ""))[:_MAX_TEXT],
        "detail": str(raw.get("detail", ""))[:_MAX_TEXT],
        "reset_at": str(raw.get("reset_at", ""))[:_MAX_TEXT],
        "limit_kind": str(raw.get("limit_kind", ""))[:_MAX_TEXT],
    }
    return name, row


def read() -> dict[str, PeerStatus]:
    """Every recorded peer's last-observed status. Never raises.

    Detail is re-bounded on read so a file written by an older/other build cannot
    push an oversized blob into a health snapshot (and from there into an LLM
    context via the health MCP tool).
    """
    out: dict[str, PeerStatus] = {}
    for name, raw in _read_raw().items():
        if not isinstance(raw, dict):
            continue
        key, row = _sanitise(name, raw)
        out[key] = PeerStatus(peer=key, **row)
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
    """
    try:
        if not peer or not _is_provider_refusal(exc):
            return False
        detail = str(exc)
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
        detail=detail,
        reset_at=reset_at,
        limit_kind=limit_kind,
    )


def note_success(peer: str) -> bool:
    """Record that the peer served a turn, clearing any stale block. Never raises."""
    return _record(peer, available=True)


#: Sidecar lock for the read-modify-write in :func:`_record`. A SEPARATE file on
#: purpose: the state file is published with ``os.replace``, so a lock held on
#: its inode would be released the moment a writer swapped it — the lock has to
#: outlive the file it protects.
_LOCK_FILE = "cc_peer_availability.lock"

#: Lock acquisition is NON-BLOCKING with a bounded retry, never a bare LOCK_EX.
#: This runs synchronously inside an async failover turn, so a blocking flock
#: stalls the whole event loop — advisory bookkeeping would then delay every
#: concurrent conversation during the outage it exists to observe, which is a
#: worse failure than the lost row it prevents. 10 x 20ms caps the stall at
#: ~200ms under contention, after which it degrades open exactly as documented.
_LOCK_ATTEMPTS = 10
_LOCK_SLEEP_S = 0.02


@contextlib.contextmanager
def _record_lock():
    """Serialize read-modify-write across processes. Never raises.

    Without it the sequence is last-writer-wins: two processes recording
    DIFFERENT peers in the same instant each read the map, each add their row,
    and the second write drops the first. An earlier draft accepted that on the
    grounds it was advisory and self-healing — but "self-healing" was itself a
    claim that had to be retracted (records refresh only during a home outage),
    so a lost record can persist for days, and losing an observation is exactly
    the silence this module exists to remove.

    Degrades OPEN: if the lock cannot be taken (a filesystem without flock, a
    permissions problem), the write still proceeds unserialized rather than
    being dropped. A racy record beats no record on an advisory path.
    """
    path = _state_path().parent / _LOCK_FILE
    fh = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fh = path.open("a+")
        for attempt in range(_LOCK_ATTEMPTS):
            try:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if attempt == _LOCK_ATTEMPTS - 1:
                    raise
                time.sleep(_LOCK_SLEEP_S)
    except Exception:
        logger.debug("peer availability lock unavailable — proceeding unserialized", exc_info=True)
        if fh is not None:
            with contextlib.suppress(Exception):
                fh.close()
            fh = None
    try:
        yield
    finally:
        if fh is not None:
            with contextlib.suppress(Exception):
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            with contextlib.suppress(Exception):
                fh.close()


def _record(
    peer: str,
    *,
    available: bool,
    reason: str = "",
    detail: str = "",
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
            detail="" if available else _scrub(detail),
            reset_at="" if available else reset_at,
            limit_kind="" if available else limit_kind,
        )
        # Merge onto a SANITISED view of what is on disk, under the lock so a
        # concurrent recorder cannot drop this row. Writing _read_raw()
        # straight back would (a) crash the eviction sort below on a non-dict row
        # — permanently killing recording, because the failure is swallowed by
        # the except clause and note_failure just returns False forever — and
        # (b) re-persist an oversized detail written by another build, since
        # _MAX_TEXT otherwise bounds only the value being written now.
        with _record_lock():
            return _merge_and_write(peer, status)
    except Exception:
        logger.debug("peer availability record failed for %s", peer, exc_info=True)
        return False


def _merge_and_write(peer: str, status: PeerStatus) -> bool:
    """Read-modify-write the peer map. Caller holds :func:`_record_lock`.

    Merges onto a SANITISED view of what is on disk. Writing ``_read_raw()``
    straight back would re-persist a non-dict row (which older eviction logic
    then choked on) and an oversized detail written by another build, since
    ``_MAX_TEXT`` otherwise bounds only the value being written now.
    """
    peers = dict(
        _sanitise(k, v) for k, v in _read_raw().items() if isinstance(v, dict)
    )
    peer, row = _sanitise(peer, {k: v for k, v in asdict(status).items() if k != "peer"})
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
