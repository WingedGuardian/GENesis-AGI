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
bounded, because bounding first lets a secret straddling the bound survive as an
unmatched prefix, and the scrub fails CLOSED (prose is dropped) when the roster
cannot be read to discover custom-named credentials.

WHY THE SCRUB MATTERS, precisely: this file is NOT captured by backups
(``scripts/backup.sh`` is selective and takes no blanket ``~/.genesis/*.json``).
It is read into every health snapshot, reaches an LLM's context through the
health MCP tool, and ``sentinel/monitor.py`` JSON-dumps the whole ``cc_sessions``
payload — provider ``detail`` included — into a monitoring prompt with no
sanitisation on that path. That is the exposure the scrub exists for.

NOTHING IS EVER TRUNCATED. A value is stored whole or replaced by a fixed-size
marker naming what was dropped. A truncated value still LOOKS complete, so
nobody checks it; and truncating the peer NAME merged two peers onto one key,
letting one peer's success clear another's recorded failure. Bounds here are
per-FIELD and derived from what each field means (see ``_sanitise``).

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

#: Bound on stored provider prose — the point at which a value stops being a
#: MESSAGE and starts being a BODY. Above it the whole value is replaced by a
#: fixed-size marker; below it the value is kept WHOLE. Never a mid-value cut.
#:
#: DERIVED, not invented. Measured over 60 days of a live install's server log,
#: 183/670 (27.3%) of real refusal messages exceed the 300-char cap an earlier
#: draft used, and 0/670 exceed this one (observed max 1057) — a bound that
#: amputates a quarter of the real population makes the field pointless, which
#: is the only reason the field exists. The corpus is refusal-concept prose from
#: one install, so it bounds the SHAPE of provider text, not this exact call
#: site's population; the headroom above the observed max is deliberate.
_MAX_DETAIL = 2000

#: Emitted INSTEAD of an oversized value. Fixed size, states what was dropped,
#: and carries no fragment of the original: a truncated value still LOOKS
#: complete so nobody checks it, where an explicit gap invites the question.
_OMITTED_TEXT = "<omitted: {n} chars of provider text — too large to store>"

#: Emitted instead of `detail` when credential discovery failed (see _scrub).
_OMITTED_UNSAFE = "<omitted: credential discovery unavailable — see logs>"

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

#: The only reason recorded. A named constant so the recorder, the snapshot and
#: any future renderer agree on the string. No dashboard template consumes it
#: today — verified, not assumed.
QUOTA = "quota"

#: Closed sets for the two enum-valued fields. ``_LIMIT_KINDS`` is DERIVED from
#: the parser that produces the value rather than re-typed here, so the two
#: cannot drift apart: a kind added there is accepted here without an edit.
_REASONS = frozenset({"", QUOTA})


def _load_limit_kinds() -> frozenset[str]:
    # Resolved ONCE at import, not per row. A deferred import inside _sanitise
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
    # "" when available. When NOT available it is QUOTA — or "" if the recorded
    # reason came from disk and was not an interpretable value, which is stated
    # here rather than repaired: inventing QUOTA would fabricate an observation.
    reason: str = ""
    observed_at: str = ""  # ISO8601 UTC of the observation
    detail: str = ""  # bounded, scrubbed provider text
    reset_at: str = ""  # ISO8601 when the provider's reset time was parseable
    limit_kind: str = ""  # session / weekly / unknown, per rate_limit_reset


def _secret_names() -> tuple[set[str], bool]:
    """Credential-bearing env var names, and whether discovery was COMPLETE.

    Name hints, PLUS every ``auth_env`` the roster itself declares. Those names
    are user-chosen (``auth_env: GLM_PAT``) and need not contain any hint word,
    so hint-matching alone would write a peer's own token to disk verbatim —
    precisely the credential most likely to appear in that peer's error text.

    The second element is FALSE when the roster could not be read, and it is the
    whole point of the tuple. Falling back to hints alone is silently unsafe: it
    looks like a scrub and is not one for exactly the custom-named credential
    this function exists to catch.
    """
    names = {n for n in os.environ if any(h in n.upper() for h in _SECRET_NAME_HINTS)}
    try:
        from genesis.cc import roster

        models = roster.load_roster().get("models")
    except Exception:
        logger.warning("roster import/load raised during scrub", exc_info=True)
        return names, False
    if not isinstance(models, dict) or not models:
        # KEYING ON THE RESULT, NOT ON A RAISE. `roster._load_yaml` swallows every
        # read failure and returns {}, and `merge_local_overlay` degrades the same
        # way — so `load_roster()` never raises for the cause this guard names,
        # and a guard keyed on "it raised" would be DEAD on arrival. The shipped
        # `config/cc_roster.yaml` always declares at least the native model, so
        # an empty models map means we could not read the roster, not that the
        # user has none. Safe to treat as incomplete: no models also means no
        # peers, so no observation is lost by dropping prose here.
        logger.warning(
            "roster declared no models — dropping peer detail rather than "
            "persisting text a custom-named credential may appear in",
        )
        return names, False
    for raw in models.values():
        if isinstance(raw, dict) and raw.get("auth_env"):
            names.add(str(raw["auth_env"]))
    return names, True


def _secret_values() -> tuple[list[str], bool]:
    """Credential values worth redacting, LONGEST FIRST, + discovery completeness.

    Order matters: when one secret contains another (a token and a prefix of it
    both set in the environment), redacting the shorter first would leave the
    longer one's tail behind as an unmatched fragment.
    """
    names, discovery_ok = _secret_names()
    out = []
    for name in names:
        value = os.environ.get(name)
        if value and len(value) >= _MIN_SECRET_LEN:
            out.append(value)
    return sorted(out, key=len, reverse=True), discovery_ok


def _bound_text(text: str) -> str:
    """Keep a value WHOLE, or omit it wholesale. Never a partial value.

    Truncation is the absence of a decision: it cuts at a point that has nothing
    to do with meaning, and the result still LOOKS complete, so nobody checks it.
    Either the value is a message we can store (the overwhelming majority — see
    ``_MAX_DETAIL``) or it is a body we should not, and saying so plainly is
    more useful than a plausible fragment of it.
    """
    return text if len(text) <= _MAX_DETAIL else _OMITTED_TEXT.format(n=len(text))


def _secret_context() -> tuple[list[str], bool]:
    """The scrub context for ONE operation. Never raises.

    Hoisted out of ``_scrub`` so a read or a merge resolves the roster ONCE
    rather than once per row — ``read()`` runs on the health snapshot path and
    would otherwise reload the roster for every peer it returns.
    """
    try:
        return _secret_values()
    except Exception:
        # Fail CLOSED on the prose, never on the observation: letting this
        # escape would be swallowed by _record and drop the whole row, losing
        # the availability signal over a fault in an auxiliary redaction step.
        logger.warning("credential discovery raised — dropping detail", exc_info=True)
        return [], False


def _scrub_with(text: str, values: list[str], discovery_ok: bool) -> str:
    """Redact, THEN bound, against an already-resolved secret context."""
    out = (text or "").strip()
    if not out:
        return ""
    if not discovery_ok:
        return _OMITTED_UNSAFE
    for value in values:
        if value in out:
            out = out.replace(value, "<redacted>")
    return _bound_text(out)


def _scrub(text: str) -> str:
    """Redact credential values, THEN bound. Fails CLOSED on unknown credentials.

    Order is load-bearing and was a real defect: bounding first let a secret that
    straddled the bound lose its tail, so it no longer matched the environment
    value and its head was written to disk verbatim (reproduced with a 64-char
    token at offset 290). The file reaches the health snapshot and, through the
    health MCP tool, an LLM's context — so a partial-prefix leak is a real leak.

    When credential discovery is INCOMPLETE the prose is dropped entirely. A
    peer's own token is the credential most likely to appear in that peer's error
    text, and its env var name is user-chosen, so a hints-only pass cannot rule
    it out. Losing diagnostic prose is recoverable; leaking a token is not.
    """
    values, discovery_ok = _secret_context()
    return _scrub_with(text, values, discovery_ok)


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


def _valid_stamp(value: str) -> str:
    """An ISO8601 stamp, or "" — never a fragment of one.

    A timestamp is a SHAPE, not a length. Half a timestamp is not a shorter
    timestamp; it is a value every reader will fail to parse, which is why
    ``age_seconds`` is documented to go None rather than 0 on one.
    """
    try:
        datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return ""
    return value


def _enum(value: str, allowed: frozenset[str]) -> str:
    """A member of a closed set, or "".

    A value outside the set is INVALID, not "too long". Cutting 100,000
    characters of foreign data down to 300 characters of foreign data answers
    the wrong question and calls the result bounded.
    """
    return value if value in allowed else ""


def _sanitise(peer: str, raw: dict, secrets: tuple[list[str], bool]) -> tuple[str, dict] | None:
    """Validate a (peer, row) pair, or None to DROP it. The single chokepoint.

    Each field is bounded by what it MEANS. ``reason`` and ``limit_kind`` are
    closed enums, ``observed_at``/``reset_at`` are timestamps, and ``detail`` is
    the only genuinely free text — so it is the only field with a length bound at
    all, and even that one omits rather than cuts.

    ``detail`` is SCRUBBED here, not only where a record is written. The scrub
    was previously write-only, so a row left by an older build — or by this build
    in an environment where the credential was not discoverable — was handed
    verbatim to every health snapshot, from there into an LLM context via the
    health MCP tool and into ``sentinel/monitor.py``'s unsanitised monitoring
    prompt, and was re-persisted forever by the next merge. Reader and writer
    both go through here precisely so they cannot disagree about that.

    A row is DROPPED (not repaired) when its peer name exceeds the sanity bound.
    Truncating the key is what let two distinct peers collide, so one peer's
    success cleared another peer's recorded failure — a correctness bug
    manufactured by the cap that was supposed to be protective. Reader and writer
    both go through here so the two can never disagree about what is storable.
    """
    name = str(peer)
    if not name:
        logger.debug("dropping peer availability row: empty peer name")
        return None
    if len(name) > _MAX_PEER_NAME:
        logger.debug(
            "dropping peer availability row: name is %d chars (cap %d)",
            len(name), _MAX_PEER_NAME,
        )
        return None
    row = {
        "available": bool(raw.get("available", True)),
        "reason": _enum(str(raw.get("reason", "")), _REASONS),
        "observed_at": _valid_stamp(str(raw.get("observed_at", ""))),
        "detail": _scrub_with(str(raw.get("detail", "")), *secrets),
        "reset_at": _valid_stamp(str(raw.get("reset_at", ""))),
        "limit_kind": _enum(str(raw.get("limit_kind", "")), _LIMIT_KINDS),
    }
    return name, row


def read() -> dict[str, PeerStatus]:
    """Every recorded peer's last-observed status. Never raises.

    Detail is re-bounded on read so a file written by an older/other build cannot
    push an oversized blob into a health snapshot (and from there into an LLM
    context via the health MCP tool).
    """
    out: dict[str, PeerStatus] = {}
    dropped = 0
    try:
        secrets = _secret_context()
        for name, raw in _read_raw().items():
            if not isinstance(raw, dict):
                dropped += 1
                continue
            pair = _sanitise(name, raw, secrets)
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
    """Record that the peer served a turn, clearing any stale block. Never raises.

    Synchronous — call via ``asyncio.to_thread`` from async code.
    """
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
            # NOT scrubbed here — _sanitise is the single chokepoint and scrubs
            # on the way to disk, so reader and writer cannot diverge. This value
            # never reaches the file unscrubbed.
            detail="" if available else detail,
            reset_at="" if available else reset_at,
            limit_kind="" if available else limit_kind,
        )
        # Resolve the scrub context BEFORE taking the lock. It reads the roster
        # CONFIG, not the state file, so it does not need serializing — and doing
        # it inside the lock lengthened the critical section by a YAML read+parse,
        # which pushed contending writers past their bounded retry budget so they
        # degraded OPEN and dropped a row. MEASURED: 1 of 12 concurrent recorders
        # lost its observation until this moved out.
        secrets = _secret_context()
        # Merge onto a SANITISED view of what is on disk, under the lock so a
        # concurrent recorder cannot drop this row. Writing _read_raw()
        # straight back would (a) crash the eviction sort below on a non-dict row
        # — permanently killing recording, because the failure is swallowed by
        # the except clause and note_failure just returns False forever — and
        # (b) re-persist an oversized detail written by another build, since
        # the per-field validation otherwise applies only to the value
        # being written now.
        with _record_lock():
            return _merge_and_write(peer, status, secrets)
    except Exception:
        logger.debug("peer availability record failed for %s", peer, exc_info=True)
        return False


def _merge_and_write(peer: str, status: PeerStatus, secrets: tuple[list[str], bool]) -> bool:
    """Read-modify-write the peer map. Caller holds :func:`_record_lock`.

    Merges onto a SANITISED view of what is on disk. Writing ``_read_raw()``
    straight back would re-persist a non-dict row (which older eviction logic
    then choked on) and an oversized detail written by another build, since
    per-field validation otherwise applies only to the value being written now.
    """
    peers = {}
    for k, v in _read_raw().items():
        if not isinstance(v, dict):
            continue
        pair = _sanitise(k, v, secrets)
        if pair is not None:
            peers[pair[0]] = pair[1]
    pair = _sanitise(peer, {k: v for k, v in asdict(status).items() if k != "peer"}, secrets)
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
