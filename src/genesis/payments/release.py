"""Card release: one card's values, once, after the owner's Telegram tap.

Four socket ops, reachable only by a peer that ``peer_check`` verified as the
genesis-health MCP server (``vault_service._handle`` runs that check):

- ``release_request {card_ref, host, page_title, amount, claim_hash,
  field_origins}`` -> ``{nonce}``. Checks the request, shows the owner a tap
  (``approval.PaymentApprovalRegistry.request``) and returns at once.
- ``release_status {nonce}`` -> ``{state, expires_in_s}``, the state one of
  ``pending``, ``granted``, ``refused``, ``expired``. A claimed release reads
  ``expired`` (used up); a cancelled or unknown one reads ``refused``.
- ``release_claim {nonce, claim}`` -> the card's fields, once: only after a
  grant, within ``CLAIM_WINDOW_S`` of it, from the requesting process (same
  pid and start time), and only when ``sha256(claim)`` is the request's
  ``claim_hash``.
- ``release_cancel {nonce}``, from the requesting process.

There is one slot for the whole service. A second request while one is
pending or granted refuses both and tells the owner; while a claim is being
handed over, only the newcomer is refused. A lock, an idle relock, a stale key
or a stop refuses the release. The vault owns the deadline: a pending release
expires ``PENDING_BACKSTOP_S`` after its request even if the approval task
never returns, and every op first settles a release that is due or whose
requester has exited.

The claim decrypts the card in a thread (``vault_store.open_card``) and
re-checks the host rule against the blob's authenticated merchant. It refuses
a card past its sealed expiry month (UTC, the rule ``vault add`` applies), and
a card whose label and last four are no longer the ones the tap showed
(``open_card`` binds the last four to the sealed number). Once it
passes the epoch check after the decrypt, the claim completes: a lock landing
later does not take back values already decided. A single-use card is retired
after the ``release_claimed`` row and before the reply; if that fails nothing
is returned.

The requester is checked again at the handover: after the decrypt and again
after the ``release_claimed`` row, the claim is refused ("requester gone"), with
nothing retired and no values sent, if the requesting process has exited
(pid and start time) or its connection has closed (``vault_service``'s
docstring says what a client must do for that check to see it). One window is left: a
connection that closes after the second check, during the retire or the reply
itself, still burns a single-use card whose values never arrive. That fails
safe (a lost card, never a card handed to someone else).

Every request, grant, claim, refusal and cancel is a value-free row in
``payment_vault_events``: nonce, card ref, host, the amount and title as shown,
the field-origin hosts, the verified peer and a fixed reason. A refused request
records only ``{reason, peer}``, never the input it rejected. Card values exist
only inside the claim's reply. Memory residue is not claimed away: pyrage's
plaintext, the decoded strings and the reply buffers stay in the process heap
until reused.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import re
import secrets
import unicodedata
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from genesis.payments import approval
from genesis.payments import vault_release_store as vrs
from genesis.payments import vault_store as vs
from genesis.payments.peer_check import LAUNCHED, Peer, start_of

logger = logging.getLogger(__name__)

APPROVAL_TIMEOUT_S = approval.DEFAULT_TIMEOUT_S
# The vault's own deadline for a pending release, past the registry's: an
# approval task that hangs or dies cannot hold the slot.
PENDING_BACKSTOP_S = APPROVAL_TIMEOUT_S + 120
CLAIM_WINDOW_S = 120
_CHECK_EVERY_S = 30.0  # loop time; the deadline itself is on the service clock
_RECENT = 16
_TITLE_CHARS = 80
_AMOUNT_CHARS = 32
_ORIGIN_KEYS = frozenset(
    {"number", "expiry", "exp_month", "exp_year", "cvc", "name", "postal_code"}
)
_CLAIM_HASH = re.compile(r"[0-9a-f]{64}")
_NONCE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_ORIGIN = re.compile(r"(https?)://([^/:?#@]+)(?::([0-9]{1,5}))?")

PENDING, GRANTED, CLAIMING = "pending", "granted", "claiming"
CLAIMED, EXPIRED, CANCELLED, REFUSED = "claimed", "expired", "cancelled", "refused"
_SHOWN = {CLAIMED: "expired", EXPIRED: "expired", CANCELLED: "refused", REFUSED: "refused"}


class _Refusal(Exception):
    """A request refused with a fixed reason."""


@dataclass(eq=False)
class _Release:
    nonce: str
    card_ref: str
    host: str
    claim_hash: str
    peer: Peer
    epoch: int
    deadline: float  # on the service clock
    card_shown: tuple[str, str]  # (label, last4) as the tap showed them
    state: str = PENDING
    decided: bool = False  # a claim past its epoch check completes
    task: asyncio.Task | None = None
    timer: asyncio.TimerHandle | None = None
    row: dict = field(default_factory=dict)  # value-free facts every row repeats


def _sanitise_title(title: str) -> str:
    text = unicodedata.normalize("NFC", title)
    # Drops controls, format characters (bidi overrides, zero-width joiners),
    # surrogates, private-use and unassigned code points.
    # A newline or tab separates words; it becomes a space, then whitespace collapses.
    text = "".join(
        " " if ch.isspace() else ch
        for ch in text
        if ch.isspace() or not unicodedata.category(ch).startswith("C")
    )
    return (" ".join(text.split()) or "(no title)")[:_TITLE_CHARS]


def _host(value) -> str:
    if not isinstance(value, str) or not value.isascii():
        raise _Refusal("bad host")
    try:
        return vs.normalise_merchant(value)
    except vs.VaultError:
        raise _Refusal("bad host") from None


def _origins(value) -> dict[str, str]:
    """field -> how the tap shows its origin: the host, with the scheme unless https."""
    if not isinstance(value, dict) or not 0 < len(value) <= len(_ORIGIN_KEYS):
        raise _Refusal("bad field_origins")
    shown = {}
    for key in sorted(value, key=str):
        origin = value[key]
        m = _ORIGIN.fullmatch(origin) if isinstance(origin, str) else None
        if key not in _ORIGIN_KEYS or m is None:
            raise _Refusal("bad field_origins")
        scheme, port = m.group(1), m.group(3)
        try:
            host = _host(m.group(2))
        except _Refusal:
            raise _Refusal("bad field_origins") from None
        if port is not None and not 0 < int(port) < 65536:
            raise _Refusal("bad field_origins")
        text = host if scheme == "https" else f"{scheme}://{host}"
        shown[key] = text + (f":{int(port)}" if port else "")
    return shown


def _now() -> datetime:
    return datetime.now(UTC)


def _in_scope(host: str, merchant: str | None, single_use: bool) -> bool:
    """The host rule. A merchant scope always binds; only an unscoped single-use card skips it."""
    if merchant is not None:
        return host == merchant or host.endswith("." + merchant)
    return single_use


class ReleaseManager:
    """The one-slot release state machine; the vault service owns one."""

    def __init__(
        self,
        service,
        approvals=None,
        *,
        alive: Callable[[int, int], bool] | None = None,
        proc_root: Path = Path("/proc"),
    ) -> None:
        self._svc = service
        self._approvals = approvals
        self._alive = alive or (lambda pid, start: start_of(pid, proc_root) == start)
        self._slot: _Release | None = None
        self._recent: OrderedDict[str, str] = OrderedDict()
        self._request_lock = asyncio.Lock()

    # -- rows, tasks, endings ------------------------------------------------

    async def _row(self, event: str, detail: dict) -> bool:
        return await self._svc._audit(event, detail)

    def _spawn(self, coro, name: str) -> asyncio.Task:
        from genesis.util.tasks import tracked_task

        task = tracked_task(coro, name=name)
        self._svc._tasks.add(task)
        task.add_done_callback(self._svc._tasks.discard)
        return task

    def _notify(self, text: str) -> None:
        if self._approvals is not None:
            self._spawn(self._approvals.notify(text), "payment-release-notify")

    def _end(self, rel: _Release, state: str) -> bool:
        """Settle ``rel`` (synchronously, before any await); False if it was not the slot."""
        if self._slot is not rel:
            return False
        self._slot = None
        rel.state = state
        if rel.timer is not None:
            rel.timer.cancel()
            rel.timer = None
        if rel.task is not None and rel.task is not asyncio.current_task():
            rel.task.cancel()  # a pending tap's button then answers "Refused"
        self._remember(rel.nonce, state)
        return True

    def _remember(self, nonce: str, state: str) -> None:
        self._recent[nonce] = state
        while len(self._recent) > _RECENT:
            self._recent.popitem(last=False)

    async def _refuse_slot(self, rel: _Release, reason: str, state: str = REFUSED) -> None:
        if self._end(rel, state):
            await self._row("release_refused", {**rel.row, "reason": reason})

    async def abort(self, reason: str) -> None:
        """Refuse the release in the slot (lock, relock, stop). A decided claim completes."""
        rel = self._slot
        if rel is not None and not rel.decided:
            await self._refuse_slot(rel, reason)

    def _arm(self, rel: _Release) -> None:
        if rel.timer is not None:
            rel.timer.cancel()
        wait = min(max(0.0, rel.deadline - self._svc._clock()), _CHECK_EVERY_S)
        rel.timer = asyncio.get_running_loop().call_later(wait, self._tick, rel)

    def _tick(self, rel: _Release) -> None:
        rel.timer = None
        if self._slot is rel:
            self._spawn(self._expire_if_due(), "payment-release-expiry")

    async def _expire_if_due(self) -> None:
        """Settle a release that is past its deadline, or whose requester exited."""
        rel = self._slot
        if rel is None or rel.state == CLAIMING:
            return
        if self._svc._clock() > rel.deadline:
            why = "approval expired" if rel.state == PENDING else "not claimed within 2 minutes"
            await self._refuse_slot(rel, why, EXPIRED)
        elif not self._alive(rel.peer.pid, rel.peer.start):
            await self._refuse_slot(rel, "requester gone")
        elif rel.timer is None:
            self._arm(rel)

    # -- the ops -------------------------------------------------------------

    async def handle(
        self, op: str, request: dict, peer: Peer, client_gone: Callable[[], bool] | None = None
    ) -> dict:
        """``client_gone`` says whether the requester's connection has closed."""
        if op == "release_request":
            return await self.request(request, peer)
        if op == "release_status":
            return await self.status(request)
        if op == "release_claim":
            return await self.claim(request, peer, client_gone or (lambda: False))
        if op == "release_cancel":
            return await self.cancel(request, peer)
        return {"ok": False, "error": "bad request"}

    async def _refuse(self, reason: str, peer: Peer, nonce: str | None = None) -> dict:
        detail = {"reason": reason, "peer": peer.detail()}
        if nonce is not None:
            detail["nonce"] = nonce
        await self._row("release_refused", detail)
        return {"ok": False, "error": reason}

    async def request(self, request: dict, peer: Peer) -> dict:
        await self._expire_if_due()
        async with self._request_lock:
            return await self._request(request, peer)

    async def _request(self, request: dict, peer: Peer) -> dict:
        rel = self._slot
        if rel is not None:
            if rel.state == CLAIMING:  # the claim in flight already passed its checks
                self._notify(
                    "A second card release was requested while a card was being handed "
                    "over. The second request was refused; the first goes ahead."
                )
                return await self._refuse("another release is in progress", peer)
            self._notify(
                "Two card releases were requested at once. Both were refused; "
                "any approval button already sent no longer works."
            )
            await self._refuse_slot(rel, "a second release was requested")
            return await self._refuse("a second release was requested", peer)
        svc = self._svc
        epoch = svc._epoch
        try:
            if svc._stopped:
                raise _Refusal("the vault service is stopping")
            if svc._db is None:
                raise _Refusal("the audit log is unavailable")
            if self._approvals is None:
                raise _Refusal("approval is unavailable")
            if not svc.unlocked:
                raise _Refusal("the vault is locked")
            card_ref, claim_hash = request.get("card_ref"), request.get("claim_hash")
            if not isinstance(card_ref, str) or not vs.CARD_REF.fullmatch(card_ref):
                raise _Refusal("bad card_ref")
            if not isinstance(claim_hash, str) or not _CLAIM_HASH.fullmatch(claim_hash):
                raise _Refusal("bad claim_hash")
            host = _host(request.get("host"))
            amount, title = request.get("amount"), request.get("page_title")
            if not (
                isinstance(amount, str)
                and 0 < len(amount) <= _AMOUNT_CHARS
                and amount.isprintable()
                and amount.strip()
            ):
                raise _Refusal("bad amount")
            if not isinstance(title, str):
                raise _Refusal("bad page_title")
            title = _sanitise_title(title)
            origins = _origins(request.get("field_origins"))
            try:
                cards = await asyncio.to_thread(vs.list_cards, svc._vault)
            except (vs.VaultError, OSError):
                raise _Refusal("the vault could not be read") from None
            row = next(
                (c for c in cards if c["card_ref"] == card_ref and c["status"] == "active"), None
            )
            if row is None:
                raise _Refusal("no such active card")
            if not _in_scope(host, row["merchant"], bool(row["single_use"])):
                raise _Refusal("the host is outside the card's merchant")
            session = "Genesis-launched session" if peer.kind == LAUNCHED else "foreground session"
            fields = {
                "Site": host,
                "Amount": f"{amount} (as stated by Genesis)",
                "Card": f"{row['label']} ending {row['last4']}",
                "Requester": f"{peer.comm} pid {peer.pid}, {session} (claude pid {peer.claude_pid})",
                "Page title": title,
                "Fields": ", ".join(f"{k}={v}" for k, v in origins.items()),
            }
            # What the registry would refuse is refused here, before any row.
            try:
                approval.build_text(fields)
            except ValueError:
                raise _Refusal("the approval text was refused") from None
            if svc._stopped or epoch != svc._epoch or not svc.unlocked:
                raise _Refusal("the vault was locked")
        except _Refusal as exc:
            return await self._refuse(str(exc), peer)
        nonce = secrets.token_urlsafe(16)
        facts = {"nonce": nonce, "card_ref": card_ref, "host": host, "peer": peer.detail()}
        if not await self._row(
            "release_requested",
            {**facts, "amount": amount, "page_title": title, "field_origins": origins},
        ):
            return {"ok": False, "error": "the audit write failed"}
        rel = _Release(
            nonce=nonce,
            card_ref=card_ref,
            host=host,
            claim_hash=claim_hash,
            peer=peer,
            epoch=epoch,
            deadline=svc._clock() + PENDING_BACKSTOP_S,
            card_shown=(row["label"], row["last4"]),
            row=facts,
        )
        if svc._stopped or epoch != svc._epoch or not svc.unlocked:  # locked during the row
            self._remember(nonce, REFUSED)
            await self._row("release_refused", {**facts, "reason": "the vault was locked"})
            return {"ok": False, "error": "the vault was locked"}
        self._slot = rel
        self._arm(rel)
        audit_detail = {"release": nonce, "card_ref": card_ref, "host": host}
        rel.task = self._spawn(self._await_approval(rel, fields, audit_detail), "payment-release")
        return {"ok": True, "nonce": nonce}

    async def _await_approval(self, rel: _Release, fields: dict, audit_detail: dict) -> None:
        outcome, why = REFUSED, "the approval failed"
        try:
            outcome = await self._approvals.request(
                fields, audit_detail=audit_detail, timeout_s=APPROVAL_TIMEOUT_S
            )
            why = {approval.EXPIRED: "approval expired"}.get(outcome, "the owner did not approve")
        except asyncio.CancelledError:
            why = "the approval was cancelled"
            raise
        except Exception as exc:  # noqa: BLE001 - a failed approval is a refusal
            logger.error("payment release approval failed (%s)", type(exc).__name__)
        finally:
            await self._settle(rel, outcome, why)

    def _current(self, rel: _Release) -> bool:
        svc = self._svc
        return (
            self._slot is rel
            and rel.state == PENDING
            and rel.epoch == svc._epoch
            and not svc._stopped
            and svc.unlocked
        )

    async def _settle(self, rel: _Release, outcome: str, why: str) -> None:
        """The approval's outcome; a grant that lands after an abort is refused."""
        if outcome == approval.GRANTED and not self._current(rel):
            why = "the grant came after the release was refused"
        if outcome != approval.GRANTED or not self._current(rel):
            if self._slot is rel and rel.state == PENDING:
                await self._refuse_slot(
                    rel, why, EXPIRED if outcome == approval.EXPIRED else REFUSED
                )
            return
        rel.task = None  # this task is ending: nothing to cancel any more
        if not await self._row("release_granted", rel.row):
            await self._refuse_slot(rel, "the audit write failed")
            return
        if not self._current(rel):  # aborted while the row was written; the abort wrote its row
            return
        rel.state = GRANTED
        rel.deadline = self._svc._clock() + CLAIM_WINDOW_S
        self._arm(rel)

    async def status(self, request: dict) -> dict:
        await self._expire_if_due()
        nonce = request.get("nonce")
        if not isinstance(nonce, str) or not _NONCE.fullmatch(nonce):
            return {"ok": False, "error": "bad request"}
        rel = self._slot
        if rel is not None and hmac.compare_digest(rel.nonce, nonce):
            left = max(0, round(rel.deadline - self._svc._clock()))
            shown = PENDING if rel.state == PENDING else GRANTED
            return {"ok": True, "state": shown, "expires_in_s": left}
        return {
            "ok": True,
            "state": _SHOWN.get(self._recent.get(nonce), REFUSED),
            "expires_in_s": 0,
        }

    def _mine(self, nonce) -> _Release | None:
        rel = self._slot
        # Shape first: compare_digest raises TypeError on a non-ASCII str.
        ok = isinstance(nonce, str) and _NONCE.fullmatch(nonce) is not None
        if ok and rel is not None and hmac.compare_digest(rel.nonce, nonce):
            return rel
        return None

    async def claim(
        self, request: dict, peer: Peer, client_gone: Callable[[], bool] = lambda: False
    ) -> dict:
        await self._expire_if_due()
        nonce, claim = request.get("nonce"), request.get("claim")
        shown = nonce if isinstance(nonce, str) and _NONCE.fullmatch(nonce) else None
        rel = self._mine(nonce)
        if rel is None or rel.state == CLAIMING or not isinstance(claim, str):
            return await self._refuse("no such release", peer, shown)
        if (peer.pid, peer.start) != (rel.peer.pid, rel.peer.start):
            return await self._refuse("not the requesting process", peer, shown)
        digest = hashlib.sha256(claim.encode("utf-8", "surrogatepass")).hexdigest()
        if not hmac.compare_digest(digest, rel.claim_hash):
            await self._refuse_slot(rel, "wrong claim secret")  # burnt: no guessing
            return {"ok": False, "error": "wrong claim secret"}
        if rel.state != GRANTED:
            return await self._refuse("not granted", peer, shown)
        svc = self._svc
        identity, epoch = svc._identity, svc._epoch
        if identity is None:
            await self._refuse_slot(rel, "the vault is locked")
            return {"ok": False, "error": "the vault is locked"}
        rel.state = CLAIMING
        try:
            return await self._hand_over(rel, identity, epoch, client_gone)
        finally:
            # Every return of _hand_over ends the slot; only a coroutine killed outright
            # (loop teardown) gets here with it still claiming. Nothing was sent.
            if self._slot is rel and rel.state == CLAIMING:
                logger.error("payment release claim ended without settling")
                self._end(rel, REFUSED)

    def _gone(self, rel: _Release, client_gone: Callable[[], bool]) -> bool:
        """The requester exited, or closed the connection the reply would go to."""
        try:
            return bool(client_gone()) or not self._alive(rel.peer.pid, rel.peer.start)
        except Exception as exc:  # noqa: BLE001 - an unanswerable check counts as gone
            logger.error("payment release liveness check failed (%s)", type(exc).__name__)
            return True

    async def _hand_over(
        self, rel: _Release, identity, epoch: int, client_gone: Callable[[], bool]
    ) -> dict:
        """Decrypt, decide, record, retire if single-use, and return the fields once."""
        svc = self._svc
        try:
            card = await asyncio.to_thread(vrs.open_card, rel.card_ref, identity, svc._vault)
        except Exception as exc:  # noqa: BLE001 - a VaultError (fixed text) or worse: refused
            logger.error("payment release open failed (%s)", type(exc).__name__)
            await self._refuse_slot(rel, "the card could not be opened")
            return {"ok": False, "error": "the card could not be opened"}
        finally:
            del identity
        # The decision point: an abort before here wins, one after it does not.
        if self._slot is not rel or epoch != svc._epoch or svc._stopped:
            del card
            return {"ok": False, "error": "the release was refused"}
        if self._gone(rel, client_gone):  # nothing decided yet: the card stays as it was
            del card
            await self._refuse_slot(rel, "requester gone")
            return {"ok": False, "error": "requester gone"}
        rel.decided = True
        if not _in_scope(rel.host, card["merchant"], card["single_use"]):
            del card
            await self._refuse_slot(rel, "the host is outside the card's merchant")
            return {"ok": False, "error": "the host is outside the card's merchant"}
        refusal, now = None, _now()
        if (card["exp_year"], card["exp_month"]) < (now.year, now.month):
            refusal = "the card has expired"  # valid through its expiry month, as add_card has it
        elif (card["label"], card["last4"]) != rel.card_shown:
            refusal = "the card is not the one shown in the approval"
        if refusal is not None:
            del card
            await self._refuse_slot(rel, refusal)
            return {"ok": False, "error": refusal}
        if not await self._row(
            "release_claimed", {**rel.row, "pending_retire": card["single_use"]}
        ):
            del card
            await self._refuse_slot(rel, "the audit write failed")
            return {"ok": False, "error": "the audit write failed"}
        # Checked again after the row's await and before the retire: the last
        # point where a gone requester leaves a single-use card unspent.
        if self._gone(rel, client_gone):
            del card
            await self._refuse_slot(rel, "requester gone")
            return {"ok": False, "error": "requester gone"}
        if card["single_use"]:
            try:
                await asyncio.to_thread(vrs.retire_released, rel.card_ref, svc._vault)
            except Exception as exc:  # noqa: BLE001 - not retired: nothing is returned
                logger.error("payment release retire failed (%s)", type(exc).__name__)
                del card
                await self._refuse_slot(rel, "retire failed")
                return {"ok": False, "error": "retire failed"}
        self._end(rel, CLAIMED)
        keys = ("number", "exp_month", "exp_year", "cvc", "name", "postal_code", "label", "last4")
        reply = {"ok": True, "card": {k: card[k] for k in keys}}
        del card
        return reply

    async def cancel(self, request: dict, peer: Peer) -> dict:
        rel = self._mine(request.get("nonce"))
        if rel is None:
            return {"ok": False, "error": "no such release"}
        if (peer.pid, peer.start) != (rel.peer.pid, rel.peer.start):
            return await self._refuse("not the requesting process", peer, rel.nonce)
        if rel.state == CLAIMING:
            return {"ok": False, "error": "the release is being handed over"}
        if self._end(rel, CANCELLED):
            await self._row("release_cancelled", rel.row)
        return {"ok": True}
