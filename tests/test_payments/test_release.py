"""Tests for the card release state machine (payments/release.py) through the vault socket.

Only Stripe's public test Visa appears, built from parts; the holder name and
postal code are synthetic placeholders. Every test ends with a sweep that no
card value reached an audit row, a log line, a non-claim reply or the tap.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
import os
import re
import shutil
import sqlite3
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
from pyrage import encrypt, x25519
from pyrage import passphrase as age_passphrase

from genesis.db.crud import payment_vault_events as audit
from genesis.payments import approval as ap
from genesis.payments import release as rl
from genesis.payments import vault_release_store as vrs
from genesis.payments import vault_store as vs
from genesis.payments.peer_check import FOREGROUND, LAUNCHED, Peer, PeerRefused, start_of
from genesis.payments.vault_service import VaultService
from genesis.security.card_shapes import contains_card_number

PASSPHRASE = "quartz lantern ember noodle 41"
MIG = importlib.import_module("genesis.db.migrations.20261011060631_payment_vault_events")
VISA = "4242" * 4  # Stripe's public test Visa
THREEDS = "4000" + "0025" + "0000" + "3155"  # Stripe's public 3-D Secure test card
CVC = "987"
NAME = "Ada Placeholder"
POSTAL = "ZZ9 9ZZ"
SECRET = "claim-secret-for-tests"
CLAIM_HASH = hashlib.sha256(SECRET.encode()).hexdigest()
HOST = "shop.example.com"
ORIGINS = {"number": "https://js.stripe.com", "cvc": "https://js.stripe.com"}


def _exp_year() -> int:
    return datetime.now(UTC).year + 3  # read per test, never at import


def _me(**over) -> Peer:
    pid = os.getpid()
    base = dict(
        pid=pid,
        start=start_of(pid),
        comm="python",
        chain=("terse", "claude"),
        claude_pid=4321,
        kind=FOREGROUND,
    )
    return Peer(**{**base, **over})


# -- fixtures -----------------------------------------------------------------


@pytest.fixture(scope="module")
def _sealed(tmp_path_factory):
    path = tmp_path_factory.mktemp("sealed") / "payment_vault"
    vs.init_vault(PASSPHRASE, path)
    key = age_passphrase.decrypt((path / vs.IDENTITY).read_bytes(), PASSPHRASE).decode()
    return path, key


@pytest.fixture
def vault(_sealed, tmp_path):
    path = tmp_path / "payment_vault"
    shutil.copytree(_sealed[0], path)
    card = vs.parse_card(VISA, f"12/{_exp_year()}", CVC)
    refs = {
        "scoped": vs.add_card(
            card,
            label="Test card",
            merchant="example.com",
            single_use=False,
            name=NAME,
            postal_code=POSTAL,
            path=path,
        ),
        "single": vs.add_card(
            card,
            label="Once card",
            merchant=None,
            single_use=True,
            name=NAME,
            postal_code=POSTAL,
            path=path,
        ),
        "both": vs.add_card(
            card,
            label="Scoped once card",
            merchant="example.com",
            single_use=True,
            name=NAME,
            postal_code=POSTAL,
            path=path,
        ),
    }
    return path, refs


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "g.db")) as conn:
        await MIG.up(conn)
        yield conn


class FakeRegistry:
    """PaymentApprovalRegistry's contract: request(fields) awaits a test-held future."""

    def __init__(self) -> None:
        self.calls: list[tuple[dict, dict, float | None]] = []
        self.futures: list[asyncio.Future] = []
        self.notices: list[str] = []
        self.fail: BaseException | None = None

    def attach_audit(self, _audit) -> None:
        pass

    async def request(self, fields, *, audit_detail=None, timeout_s=None) -> str:
        self.calls.append((dict(fields), dict(audit_detail or {}), timeout_s))
        # The real registry's guards: what it would refuse, this refuses too.
        try:
            ap.build_text(fields)
            ap._audit_detail(audit_detail)
        except ValueError:
            return ap.REFUSED
        fut = asyncio.get_running_loop().create_future()
        self.futures.append(fut)
        if self.fail is not None:
            raise self.fail
        return await fut

    async def notify(self, text: str) -> bool:
        self.notices.append(text)
        return True


class Ctx:
    def __init__(self, svc, reg, refs, clock, db) -> None:
        self.svc, self.reg, self.refs, self.clock, self.db = svc, reg, refs, clock, db
        self.peer = _me()
        self.replies: list[dict] = []

    async def ask(self, payload: dict, raw: bytes | None = None) -> dict | None:
        reader, writer = await asyncio.open_unix_connection(str(self.svc._socket_path))
        writer.write(raw if raw is not None else (json.dumps(payload) + "\n").encode())
        await writer.drain()
        line = await reader.readline()
        writer.close()
        reply = json.loads(line) if line else None
        if reply is not None and "card" not in reply:
            self.replies.append(reply)
        return reply

    async def request(self, **over) -> dict:
        payload = {
            "op": "release_request",
            "card_ref": self.refs["scoped"],
            "host": HOST,
            "page_title": "Checkout",
            "amount": "12.00 USD",
            "claim_hash": CLAIM_HASH,
            "field_origins": ORIGINS,
        }
        payload.update(over)
        return await self.ask({k: v for k, v in payload.items() if v is not _DROP})

    async def status(self, nonce: str) -> dict:
        return await self.ask({"op": "release_status", "nonce": nonce})

    async def claim(self, nonce: str, claim: str = SECRET) -> dict:
        return await self.ask({"op": "release_claim", "nonce": nonce, "claim": claim})

    async def cancel(self, nonce: str) -> dict:
        return await self.ask({"op": "release_cancel", "nonce": nonce})

    async def granted(self, **over) -> str:
        """Request, tap Approve, and wait for the grant; the nonce."""
        nonce = (await self.request(**over))["nonce"]
        await _until(lambda: self.reg.futures)
        self.reg.futures[-1].set_result(ap.GRANTED)
        await _until(lambda: self.slot() is not None and self.slot().state == rl.GRANTED)
        return nonce

    def slot(self):
        return self.svc._release._slot

    async def events(self) -> list[dict]:
        return list(reversed(await audit.list_recent(self.db, 500)))

    async def names(self) -> list[str]:
        return [e["event"] for e in await self.events()]


_DROP = object()


async def _until(pred, timeout: float = 3.0) -> None:
    async with asyncio.timeout(timeout):
        while not pred():
            await asyncio.sleep(0.005)


@pytest.fixture
async def ctx(db, vault, tmp_path, _sealed, caplog):
    caplog.set_level(logging.DEBUG)
    path, refs = vault
    reg = FakeRegistry()
    clock = [1000.0]
    holder: dict = {}
    svc = VaultService(
        db,
        vault_path=path,
        socket_path=tmp_path / "run" / "v.sock",
        clock=lambda: clock[0],
        approvals=reg,
        release_peer=lambda pid, **_k: holder["ctx"].peer,
    )
    svc._identity = x25519.Identity.from_str(_sealed[1])  # unlocked (scrypt once per module)
    await svc.start()
    c = Ctx(svc, reg, refs, clock, db)
    holder["ctx"] = c
    yield c
    await svc.stop()
    await _sweep(c, caplog.text)


async def _sweep(c: Ctx, log_text: str) -> None:
    """No card value in any row, log line, non-claim reply, tap field or notice."""
    year = str(_exp_year())
    texts = [json.dumps(e) for e in await c.events()]
    texts += [log_text, json.dumps(c.replies)]
    texts += [json.dumps(call) for call in c.reg.calls] + c.reg.notices
    bounded = [
        re.compile(rf"(?<![0-9A-Za-z_-]){re.escape(v)}(?![0-9A-Za-z_-])")
        for v in (CVC, year, f"12/{year}", f"12/{year[2:]}")
    ]
    for text in texts:
        assert VISA not in text and VISA[4:] not in text
        assert not contains_card_number(text)
        assert NAME not in text and POSTAL not in text
        assert not any(p.search(text) for p in bounded), text[:200]


# -- PENDING ------------------------------------------------------------------------


async def test_a_request_shows_the_tap_and_reads_pending(ctx):
    reply = await ctx.request()
    assert reply["ok"] and re.fullmatch(r"[A-Za-z0-9_-]{22}", reply["nonce"])
    await _until(lambda: ctx.reg.calls)
    fields, detail, timeout_s = ctx.reg.calls[0]
    assert list(fields) == ["Site", "Amount", "Card", "Requester", "Page title", "Fields"]
    assert fields["Site"] == HOST
    assert fields["Amount"] == "12.00 USD (as stated by Genesis)"
    assert fields["Card"] == "Test card ending 4242"
    assert fields["Requester"] == (
        f"python pid {os.getpid()}, foreground session (claude pid 4321)"
    )
    assert fields["Fields"] == "cvc=js.stripe.com, number=js.stripe.com"
    assert detail == {"release": reply["nonce"], "card_ref": ctx.refs["scoped"], "host": HOST}
    assert timeout_s == rl.APPROVAL_TIMEOUT_S == ap.DEFAULT_TIMEOUT_S
    status = await ctx.status(reply["nonce"])
    assert status == {"ok": True, "state": "pending", "expires_in_s": rl.PENDING_BACKSTOP_S}
    assert rl.PENDING_BACKSTOP_S == ap.DEFAULT_TIMEOUT_S + 120
    assert (await ctx.names())[-1] == "release_requested"


async def test_a_launched_session_says_so_in_the_tap(ctx):
    ctx.peer = _me(kind=LAUNCHED)
    await ctx.request()
    await _until(lambda: ctx.reg.calls)
    assert "Genesis-launched session" in ctx.reg.calls[0][0]["Requester"]


async def test_an_early_claim_is_refused_without_burning(ctx):
    nonce = (await ctx.request())["nonce"]
    assert (await ctx.claim(nonce))["error"] == "not granted"
    assert (await ctx.status(nonce))["state"] == "pending"
    assert (await ctx.names())[-1] == "release_refused"


async def test_a_wrong_secret_burns_a_pending_release(ctx):
    nonce = (await ctx.request())["nonce"]
    await _until(lambda: ctx.reg.futures)
    assert (await ctx.claim(nonce, "guess"))["error"] == "wrong claim secret"
    assert (await ctx.status(nonce))["state"] == "refused"
    await _until(lambda: ctx.reg.futures[0].cancelled())


async def test_cancel_retires_the_button(ctx):
    nonce = (await ctx.request())["nonce"]
    await _until(lambda: ctx.reg.futures)
    assert await ctx.cancel(nonce) == {"ok": True}
    assert (await ctx.status(nonce))["state"] == "refused"
    await _until(lambda: ctx.reg.futures[0].cancelled())
    assert (await ctx.names())[-1] == "release_cancelled"


async def test_a_cancel_from_another_process_is_refused(ctx):
    nonce = (await ctx.request())["nonce"]
    ctx.peer = _me(start=_me().start - 1)
    assert (await ctx.cancel(nonce))["error"] == "not the requesting process"
    ctx.peer = _me()
    assert (await ctx.status(nonce))["state"] == "pending"


@pytest.mark.parametrize("granted", [False, True])
async def test_a_second_request_refuses_both_and_tells_the_owner(ctx, granted):
    nonce = await ctx.granted() if granted else (await ctx.request())["nonce"]
    second = await ctx.request()
    assert second == {"ok": False, "error": "a second release was requested"}
    assert (await ctx.status(nonce))["state"] == "refused"
    assert (await ctx.claim(nonce))["error"] == "no such release"
    await _until(lambda: ctx.reg.notices)
    assert len(ctx.reg.notices) == 1 and "Both were refused" in ctx.reg.notices[0]
    assert (await ctx.names())[-3:-1] == ["release_refused", "release_refused"]
    if not granted:
        await _until(lambda: ctx.reg.futures[0].cancelled())


async def test_after_a_refusal_the_slot_is_free(ctx):
    nonce = (await ctx.request())["nonce"]
    await ctx.cancel(nonce)
    assert (await ctx.request())["ok"]


@pytest.mark.parametrize(("outcome", "shown"), [(ap.REFUSED, "refused"), (ap.EXPIRED, "expired")])
async def test_the_registry_outcome_settles_the_release(ctx, outcome, shown):
    nonce = (await ctx.request())["nonce"]
    await _until(lambda: ctx.reg.futures)
    ctx.reg.futures[0].set_result(outcome)
    await _until(lambda: ctx.slot() is None)
    assert (await ctx.status(nonce))["state"] == shown
    assert (await ctx.names())[-1] == "release_refused"


async def test_an_approval_task_that_raises_refuses(ctx):
    ctx.reg.fail = RuntimeError("boom")
    nonce = (await ctx.request())["nonce"]
    await _until(lambda: ctx.slot() is None)
    assert (await ctx.status(nonce))["state"] == "refused"
    rows = await ctx.events()
    assert rows[-1]["detail"]["reason"] == "the approval failed"


async def test_an_approval_task_cancelled_from_outside_refuses(ctx):
    nonce = (await ctx.request())["nonce"]
    await _until(lambda: ctx.reg.futures)
    ctx.slot().task.cancel()
    await _until(lambda: ctx.slot() is None)
    assert (await ctx.status(nonce))["state"] == "refused"


async def test_the_vault_deadline_expires_a_pending_release_lazily(ctx):
    nonce = (await ctx.request())["nonce"]
    ctx.clock[0] += rl.PENDING_BACKSTOP_S + 1  # no timer fires: the op itself checks
    assert (await ctx.status(nonce)) == {"ok": True, "state": "expired", "expires_in_s": 0}
    rows = await ctx.events()
    assert rows[-1]["detail"]["reason"] == "approval expired"


async def test_the_expiry_timer_settles_without_any_op(ctx, monkeypatch):
    monkeypatch.setattr(rl, "_CHECK_EVERY_S", 0.01)
    nonce = (await ctx.request())["nonce"]
    ctx.clock[0] += rl.PENDING_BACKSTOP_S + 1
    ctx.svc._release._arm(ctx.slot())  # re-armed at the patched interval
    await _until(lambda: ctx.slot() is None)
    assert ctx.svc._release._recent[nonce] == rl.EXPIRED


@pytest.mark.parametrize("op", ["request", "status", "claim"])
async def test_a_requester_that_exited_frees_the_slot(ctx, op):
    ctx.peer = _me(start=_me().start + 12345)  # that (pid, start) is not alive
    nonce = (await ctx.request())["nonce"]
    ctx.peer = _me()
    if op == "request":
        assert (await ctx.request())["ok"]  # judged on an empty slot
    elif op == "status":
        assert (await ctx.status(nonce))["state"] == "refused"
    else:
        assert (await ctx.claim(nonce))["error"] == "no such release"
    assert ctx.svc._release._recent[nonce] == rl.REFUSED
    await _until(lambda: ctx.reg.futures[0].cancelled())
    reasons = [e["detail"].get("reason") for e in await ctx.events()]
    assert "requester gone" in reasons


@pytest.mark.parametrize("granted", [False, True])
async def test_a_lock_refuses_the_release(ctx, granted):
    nonce = await ctx.granted() if granted else (await ctx.request())["nonce"]
    await ctx.ask({"op": "lock"})
    assert (await ctx.status(nonce))["state"] == "refused"
    assert (await ctx.claim(nonce))["error"] == "no such release"
    reasons = [e["detail"].get("reason") for e in await ctx.events() if e["detail"]]
    assert "the vault was locked" in reasons


async def test_a_grant_after_an_abort_is_refused(ctx):
    nonce = (await ctx.request())["nonce"]
    await _until(lambda: ctx.reg.futures)
    fut = ctx.reg.futures[0]
    ctx.svc._release._slot = None  # as if aborted, but the task was never cancelled
    ctx.svc._release._recent[nonce] = rl.REFUSED
    fut.set_result(ap.GRANTED)
    await asyncio.sleep(0.05)
    assert "release_granted" not in await ctx.names()
    assert (await ctx.status(nonce))["state"] == "refused"


async def test_a_grant_landing_after_a_lock_is_refused(ctx, monkeypatch):
    nonce = (await ctx.request())["nonce"]
    await _until(lambda: ctx.reg.futures)
    ctx.svc._epoch += 1  # a lock whose abort has not yet run
    ctx.reg.futures[0].set_result(ap.GRANTED)
    await _until(lambda: ctx.slot() is None)
    assert "release_granted" not in await ctx.names()
    assert (await ctx.status(nonce))["state"] == "refused"


async def test_a_lost_grant_row_refuses(ctx, monkeypatch):
    real = ctx.svc._audit

    async def failing(event, detail=None):
        return False if event == "release_granted" else await real(event, detail)

    monkeypatch.setattr(ctx.svc, "_audit", failing)
    nonce = (await ctx.request())["nonce"]
    await _until(lambda: ctx.reg.futures)
    ctx.reg.futures[0].set_result(ap.GRANTED)
    await _until(lambda: ctx.slot() is None)
    assert (await ctx.status(nonce))["state"] == "refused"


async def test_stop_refuses_a_pending_release(db, vault, tmp_path, _sealed):
    reg = FakeRegistry()
    svc = VaultService(
        db,
        vault_path=vault[0],
        socket_path=tmp_path / "s" / "v.sock",
        approvals=reg,
        release_peer=lambda pid, **_k: _me(),
    )
    svc._identity = x25519.Identity.from_str(_sealed[1])
    await svc.start()
    c = Ctx(svc, reg, vault[1], [0.0], db)
    nonce = (await c.request())["nonce"]
    await _until(lambda: reg.futures)
    await svc.stop()
    assert svc._release._recent[nonce] == rl.REFUSED
    assert reg.futures[0].cancelled()
    names = await c.names()
    assert names[-2:] == ["release_refused", "service_stop"]


# -- GRANTED ------------------------------------------------------------------------


async def test_a_granted_claim_returns_the_card_once(ctx):
    nonce = await ctx.granted()
    status = await ctx.status(nonce)
    assert status == {"ok": True, "state": "granted", "expires_in_s": rl.CLAIM_WINDOW_S}
    reply = await ctx.claim(nonce)
    assert reply["ok"]
    card = reply["card"]
    assert card["number"] == VISA and card["cvc"] == CVC
    assert (card["exp_month"], card["exp_year"]) == (12, _exp_year())
    assert (card["name"], card["postal_code"]) == (NAME, POSTAL)
    assert (card["label"], card["last4"]) == ("Test card", "4242")
    assert set(card) == {
        "number",
        "exp_month",
        "exp_year",
        "cvc",
        "name",
        "postal_code",
        "label",
        "last4",
    }
    del card, reply
    assert (await ctx.status(nonce))["state"] == "expired"
    assert (await ctx.claim(nonce))["error"] == "no such release"
    names = [n for n in await ctx.names() if n.startswith("release_")]
    assert names[:3] == ["release_requested", "release_granted", "release_claimed"]
    claimed = [e for e in await ctx.events() if e["event"] == "release_claimed"][0]
    assert claimed["detail"]["pending_retire"] is False
    assert (await ctx.cancel(nonce))["ok"] is False  # CLAIMED: a no-op
    assert (await ctx.request())["ok"]  # the slot is free


async def test_a_claim_after_two_minutes_is_refused(ctx):
    nonce = await ctx.granted()
    ctx.clock[0] += rl.CLAIM_WINDOW_S + 1
    assert (await ctx.claim(nonce))["error"] == "no such release"
    assert (await ctx.status(nonce))["state"] == "expired"
    reasons = [e["detail"].get("reason") for e in await ctx.events() if e["detail"]]
    assert "not claimed within 2 minutes" in reasons


async def test_a_claim_just_inside_the_window_succeeds(ctx):
    nonce = await ctx.granted()
    ctx.clock[0] += rl.CLAIM_WINDOW_S
    assert (await ctx.claim(nonce))["ok"]


async def test_a_wrong_secret_burns_a_granted_release(ctx):
    nonce = await ctx.granted()
    assert (await ctx.claim(nonce, "guess"))["error"] == "wrong claim secret"
    assert (await ctx.claim(nonce))["error"] == "no such release"


async def test_another_process_neither_claims_nor_burns(ctx):
    nonce = await ctx.granted()
    ctx.peer = _me(start=_me().start - 1)
    assert (await ctx.claim(nonce))["error"] == "not the requesting process"
    assert (await ctx.claim(nonce, "guess"))["error"] == "not the requesting process"
    ctx.peer = _me()
    assert (await ctx.claim(nonce))["ok"]


async def test_cancel_a_granted_release(ctx):
    nonce = await ctx.granted()
    assert (await ctx.cancel(nonce))["ok"]
    assert (await ctx.claim(nonce))["error"] == "no such release"


async def test_two_concurrent_claims_give_the_card_once(ctx):
    nonce = await ctx.granted()
    replies = await asyncio.gather(ctx.claim(nonce), ctx.claim(nonce))
    assert sum(1 for r in replies if r.get("ok")) == 1
    assert [r.get("error") for r in replies if not r.get("ok")] == ["no such release"]


async def test_a_single_use_card_is_retired_before_the_reply(ctx):
    ctx.peer = _me()
    nonce = (await ctx.request(card_ref=ctx.refs["single"], host="any.example.org"))["nonce"]
    await _until(lambda: ctx.reg.futures)
    ctx.reg.futures[0].set_result(ap.GRANTED)
    await _until(lambda: ctx.slot().state == rl.GRANTED)
    assert (await ctx.claim(nonce))["ok"]
    with sqlite3.connect(ctx.svc._vault / vs.CARDS) as conn:
        row = conn.execute(
            "SELECT status, blob IS NULL FROM cards WHERE card_ref = ?", (ctx.refs["single"],)
        ).fetchone()
    assert row == ("retired", 1)
    claimed = [e for e in await ctx.events() if e["event"] == "release_claimed"][0]
    assert claimed["detail"]["pending_retire"] is True


async def test_a_failed_retire_returns_nothing(ctx, monkeypatch):
    def broken(*_a, **_k):
        raise vs.VaultError("the vault is busy (another vault command holds it)")

    monkeypatch.setattr(vrs, "retire_released", broken)
    nonce = (await ctx.request(card_ref=ctx.refs["single"], host="any.example.org"))["nonce"]
    await _until(lambda: ctx.reg.futures)
    ctx.reg.futures[0].set_result(ap.GRANTED)
    await _until(lambda: ctx.slot().state == rl.GRANTED)
    assert await ctx.claim(nonce) == {"ok": False, "error": "retire failed"}
    names = await ctx.names()
    assert names[-2:] == ["release_claimed", "release_refused"]


async def test_a_lost_claimed_row_returns_nothing(ctx, monkeypatch):
    nonce = await ctx.granted()
    real = ctx.svc._audit

    async def failing(event, detail=None):
        return False if event == "release_claimed" else await real(event, detail)

    monkeypatch.setattr(ctx.svc, "_audit", failing)
    assert await ctx.claim(nonce) == {"ok": False, "error": "the audit write failed"}


async def test_a_card_retired_by_the_cli_meanwhile_is_not_released(ctx):
    nonce = await ctx.granted()
    vs.retire_card(ctx.refs["scoped"], PASSPHRASE, ctx.svc._vault)
    assert await ctx.claim(nonce) == {"ok": False, "error": "the card could not be opened"}
    assert (await ctx.status(nonce))["state"] == "refused"


async def test_the_claim_rechecks_the_host_against_the_blob(ctx, monkeypatch):
    nonce = await ctx.granted()
    real = vrs.open_card

    def rescoped(*a, **k):
        card = real(*a, **k)
        card["merchant"] = "other.example"
        return card

    monkeypatch.setattr(vrs, "open_card", rescoped)
    reply = await ctx.claim(nonce)
    assert reply == {"ok": False, "error": "the host is outside the card's merchant"}


async def test_a_card_past_its_expiry_month_is_refused_at_claim(ctx, monkeypatch):
    nonce = await ctx.granted(card_ref=ctx.refs["both"])
    monkeypatch.setattr(rl, "_now", lambda: datetime(_exp_year() + 1, 1, 1, tzinfo=UTC))
    assert await ctx.claim(nonce) == {"ok": False, "error": "the card has expired"}
    assert (await ctx.status(nonce))["state"] == "refused"
    assert _card_row(ctx, ctx.refs["both"]) == ("active", 0)  # nothing retired
    assert "release_claimed" not in await ctx.names()
    reasons = [(e["detail"] or {}).get("reason") for e in await ctx.events()]
    assert "the card has expired" in reasons


async def test_a_card_in_its_expiry_month_is_still_released(ctx, monkeypatch):
    nonce = await ctx.granted()
    monkeypatch.setattr(rl, "_now", lambda: datetime(_exp_year(), 12, 31, 23, 59, tzinfo=UTC))
    assert (await ctx.claim(nonce))["ok"]


async def test_last4_altered_in_the_clear_is_refused_at_claim(ctx):
    with sqlite3.connect(ctx.svc._vault / vs.CARDS) as conn:
        conn.execute("UPDATE cards SET last4 = '0000' WHERE card_ref = ?", (ctx.refs["scoped"],))
    nonce = await ctx.granted()
    assert ctx.reg.calls[-1][0]["Card"] == "Test card ending 0000"
    assert await ctx.claim(nonce) == {"ok": False, "error": "the card could not be opened"}
    assert (await ctx.status(nonce))["state"] == "refused"


async def test_a_card_whose_shown_label_changed_after_the_tap_is_refused(ctx):
    nonce = await ctx.granted()
    with sqlite3.connect(ctx.svc._vault / vs.CARDS) as conn:
        conn.execute("UPDATE cards SET label = 'Other' WHERE card_ref = ?", (ctx.refs["scoped"],))
    reply = await ctx.claim(nonce)
    assert reply == {"ok": False, "error": "the card is not the one shown in the approval"}
    assert "release_claimed" not in await ctx.names()


async def test_a_row_rescoped_in_the_clear_is_refused_at_claim(ctx):
    nonce = await ctx.granted()
    with sqlite3.connect(ctx.svc._vault / vs.CARDS) as conn:
        conn.execute(
            "UPDATE cards SET merchant = 'shop.example.com' WHERE card_ref = ?",
            (ctx.refs["scoped"],),
        )
    assert (await ctx.claim(nonce))["error"] == "the card could not be opened"


# -- CLAIMING ------------------------------------------------------------------------


@pytest.fixture
def gate(monkeypatch):
    """open_card held in its thread until ``release`` is set."""
    import threading

    entered, release = threading.Event(), threading.Event()
    real = vrs.open_card

    def held(*a, **k):
        entered.set()
        release.wait(5)
        return real(*a, **k)

    monkeypatch.setattr(vrs, "open_card", held)
    return entered, release


async def test_a_second_request_while_claiming_refuses_only_the_newcomer(ctx, gate):
    entered, release = gate
    nonce = await ctx.granted()
    claim = asyncio.create_task(ctx.claim(nonce))
    await _until(entered.is_set)
    assert (await ctx.status(nonce))["state"] == "granted"
    assert (await ctx.claim(nonce))["error"] == "no such release"
    assert (await ctx.cancel(nonce))["error"] == "the release is being handed over"
    assert (await ctx.request())["error"] == "another release is in progress"
    release.set()
    assert (await claim)["ok"]
    await _until(lambda: ctx.reg.notices)
    assert "the first goes ahead" in ctx.reg.notices[0]


async def test_a_lock_before_the_decision_point_aborts_the_claim(ctx, gate):
    entered, release = gate
    nonce = await ctx.granted()
    claim = asyncio.create_task(ctx.claim(nonce))
    await _until(entered.is_set)
    await ctx.ask({"op": "lock"})
    release.set()
    assert (await claim) == {"ok": False, "error": "the release was refused"}
    assert "release_claimed" not in await ctx.names()


async def test_a_lock_after_the_decision_point_does_not_take_values_back(ctx, monkeypatch):
    nonce = await ctx.granted()
    real = ctx.svc._audit
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(event, detail=None):
        if event == "release_claimed":
            entered.set()
            await release.wait()
        return await real(event, detail)

    monkeypatch.setattr(ctx.svc, "_audit", held)
    claim = asyncio.create_task(ctx.claim(nonce))
    await asyncio.wait_for(entered.wait(), 3)
    lock = asyncio.create_task(ctx.svc.lock(None))
    await asyncio.sleep(0.02)
    release.set()
    await lock
    assert (await claim)["ok"]
    assert "the vault was locked" not in [
        (e["detail"] or {}).get("reason") for e in await ctx.events()
    ]


async def test_a_claim_killed_mid_flight_frees_the_slot(ctx, gate):
    entered, release = gate
    nonce = await ctx.granted()
    task = asyncio.create_task(ctx.svc._release.claim({"nonce": nonce, "claim": SECRET}, ctx.peer))
    await _until(entered.is_set)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert ctx.slot() is None
    assert (await ctx.status(nonce))["state"] == "refused"
    assert (await ctx.request())["ok"]


# -- the requester gone mid-claim --------------------------------------------------


def _card_row(ctx, ref: str):
    with sqlite3.connect(ctx.svc._vault / vs.CARDS) as conn:
        return conn.execute(
            "SELECT status, blob IS NULL FROM cards WHERE card_ref = ?", (ref,)
        ).fetchone()


def _record_replies(ctx, monkeypatch) -> list[dict]:
    """Every reply the service writes, with the card's state when it was written."""
    sent: list[dict] = []
    real = ctx.svc._respond

    async def recorder(writer, reply):
        sent.append({"reply": reply, "row": _card_row(ctx, ctx.refs["both"])})
        await real(writer, reply)

    monkeypatch.setattr(ctx.svc, "_respond", recorder)
    return sent


async def _send_claim(ctx, nonce: str):
    reader, writer = await asyncio.open_unix_connection(str(ctx.svc._socket_path))
    payload = {"op": "release_claim", "nonce": nonce, "claim": SECRET}
    writer.write((json.dumps(payload) + "\n").encode())
    await writer.drain()
    return reader, writer


async def _assert_kept(ctx, nonce: str, sent: list[dict]) -> None:
    """The single-use card is still active, no reply carried values, the slot ended."""
    await _until(lambda: ctx.slot() is None)
    await _until(lambda: sent)
    assert _card_row(ctx, ctx.refs["both"]) == ("active", 0)
    assert all("card" not in s["reply"] for s in sent)
    assert sent[-1]["reply"] == {"ok": False, "error": "requester gone"}
    assert (await ctx.status(nonce))["state"] == "refused"
    reasons = [(e["detail"] or {}).get("reason") for e in await ctx.events()]
    assert "requester gone" in reasons
    assert (await ctx.request())["ok"]  # the slot is free


async def test_a_requester_that_exits_during_open_card_keeps_the_card(ctx, gate, monkeypatch):
    entered, release = gate
    nonce = await ctx.granted(card_ref=ctx.refs["both"])
    sent = _record_replies(ctx, monkeypatch)
    reader, writer = await _send_claim(ctx, nonce)
    await _until(entered.is_set)
    monkeypatch.setattr(ctx.svc._release, "_alive", lambda pid, start: False)
    release.set()
    assert json.loads(await reader.readline()) == {"ok": False, "error": "requester gone"}
    writer.close()
    await _assert_kept(ctx, nonce, sent)
    assert "release_claimed" not in await ctx.names()


async def test_a_socket_closed_during_open_card_keeps_the_card(ctx, gate, monkeypatch):
    entered, release = gate
    nonce = await ctx.granted(card_ref=ctx.refs["both"])
    sent = _record_replies(ctx, monkeypatch)
    _reader, writer = await _send_claim(ctx, nonce)
    await _until(entered.is_set)
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.05)  # the server's reader takes the EOF
    release.set()
    await _assert_kept(ctx, nonce, sent)
    assert "release_claimed" not in await ctx.names()


async def test_a_socket_closed_during_the_claimed_row_keeps_the_card(ctx, monkeypatch):
    nonce = await ctx.granted(card_ref=ctx.refs["both"])
    sent = _record_replies(ctx, monkeypatch)
    real = ctx.svc._audit
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(event, detail=None):
        if event == "release_claimed":
            entered.set()
            await release.wait()
        return await real(event, detail)

    monkeypatch.setattr(ctx.svc, "_audit", held)
    _reader, writer = await _send_claim(ctx, nonce)
    await asyncio.wait_for(entered.wait(), 3)
    writer.close()
    await writer.wait_closed()
    await asyncio.sleep(0.05)
    release.set()
    await _assert_kept(ctx, nonce, sent)
    names = [n for n in await ctx.names() if n.startswith("release_")]
    assert names[2:4] == ["release_claimed", "release_refused"]


async def test_a_requester_that_exits_during_the_claimed_row_keeps_the_card(ctx, monkeypatch):
    nonce = await ctx.granted(card_ref=ctx.refs["both"])
    sent = _record_replies(ctx, monkeypatch)
    real = ctx.svc._audit

    async def held(event, detail=None):
        if event == "release_claimed":
            monkeypatch.setattr(ctx.svc._release, "_alive", lambda pid, start: False)
        return await real(event, detail)

    monkeypatch.setattr(ctx.svc, "_audit", held)
    reader, writer = await _send_claim(ctx, nonce)
    assert json.loads(await reader.readline()) == {"ok": False, "error": "requester gone"}
    writer.close()
    await _assert_kept(ctx, nonce, sent)
    names = [n for n in await ctx.names() if n.startswith("release_")]
    assert names[2:4] == ["release_claimed", "release_refused"]


async def test_a_liveness_check_that_raises_counts_as_gone(ctx, monkeypatch):
    nonce = await ctx.granted(card_ref=ctx.refs["both"])
    sent = _record_replies(ctx, monkeypatch)
    calls = []

    def broken(pid, start):
        calls.append(pid)
        if len(calls) > 1:  # the claim's own expiry sweep passes; the handover check raises
            raise OSError("proc unreadable")
        return True

    monkeypatch.setattr(ctx.svc._release, "_alive", broken)
    reader, writer = await _send_claim(ctx, nonce)
    assert json.loads(await reader.readline()) == {"ok": False, "error": "requester gone"}
    writer.close()
    monkeypatch.setattr(ctx.svc._release, "_alive", lambda pid, start: True)
    await _assert_kept(ctx, nonce, sent)


async def test_a_half_closed_client_is_refused_and_keeps_the_card(ctx, monkeypatch):
    """The documented contract: a client keeps its write side open for the reply."""
    nonce = await ctx.granted(card_ref=ctx.refs["both"])
    sent = _record_replies(ctx, monkeypatch)
    reader, writer = await _send_claim(ctx, nonce)
    writer.write_eof()
    assert json.loads(await reader.readline()) == {"ok": False, "error": "requester gone"}
    writer.close()
    await _assert_kept(ctx, nonce, sent)


async def test_a_live_claim_still_retires_before_the_reply(ctx, monkeypatch):
    nonce = await ctx.granted(card_ref=ctx.refs["both"])
    sent = _record_replies(ctx, monkeypatch)
    reply = await ctx.claim(nonce)
    assert reply["ok"]
    del reply
    assert len(sent) == 1 and sent[0]["reply"]["ok"]
    assert sent[0]["row"] == ("retired", 1)  # retired before the reply was written


# -- a card ref whose 12 hex characters are all digits ---------------------------------


@pytest.fixture
def digit_ref(vault, monkeypatch):
    """A card whose generated ref is ``card_`` and 12 digits (about 0.36% of cards)."""
    path, refs = vault
    digits = "".join(str((i * 7 + 3) % 10) for i in range(12))  # built at run time
    card = vs.parse_card(VISA, f"12/{_exp_year()}", CVC)
    with monkeypatch.context() as m:
        m.setattr(vs.secrets, "token_hex", lambda n: digits)
        ref = vs.add_card(
            card,
            label="Digit ref card",
            merchant="example.com",
            single_use=False,
            name=NAME,
            postal_code=POSTAL,
            path=path,
        )
    assert ref == "card_" + digits and ap.holds_long_digit_run(ref)
    return ref


async def test_an_all_digit_card_ref_is_requested_sent_and_claimed(ctx, digit_ref):
    nonce = await ctx.granted(card_ref=digit_ref)
    assert ctx.reg.calls[-1][1]["card_ref"] == digit_ref
    reply = await ctx.claim(nonce)
    assert reply["ok"] and reply["card"]["label"] == "Digit ref card"
    del reply
    claimed = [e for e in await ctx.events() if e["event"] == "release_claimed"]
    assert claimed[0]["detail"]["card_ref"] == digit_ref


# -- unknown nonces -------------------------------------------------------------------


async def test_an_unknown_nonce(ctx):
    assert await ctx.status("nope") == {"ok": True, "state": "refused", "expires_in_s": 0}
    assert (await ctx.claim("nope"))["error"] == "no such release"
    assert (await ctx.cancel("nope"))["error"] == "no such release"
    assert (await ctx.status("bad nonce!"))["ok"] is False


@pytest.mark.parametrize("op", ["claim", "cancel"])
async def test_a_non_ascii_nonce_gets_a_fixed_reply_while_a_release_is_live(ctx, op):
    await ctx.request()
    reply = await (ctx.claim("\u00e9") if op == "claim" else ctx.cancel("\u00e9"))
    assert reply == {"ok": False, "error": "no such release"}
    if op == "claim":
        assert (await ctx.events())[-1]["event"] == "release_refused"


async def test_recent_outcomes_are_bounded(ctx):
    for _ in range(rl._RECENT + 2):
        nonce = (await ctx.request())["nonce"]
        await ctx.cancel(nonce)
    assert len(ctx.svc._release._recent) == rl._RECENT


# -- request validation: refused before any row, with a fixed reason -----------------------


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"card_ref": "card_zz"}, "bad card_ref"),
        ({"card_ref": 5}, "bad card_ref"),
        ({"card_ref": "card_000000000000"}, "no such active card"),
        ({"claim_hash": "ab"}, "bad claim_hash"),
        ({"claim_hash": CLAIM_HASH.upper()}, "bad claim_hash"),
        ({"host": "shöp.example.com"}, "bad host"),
        ({"host": "10.0.0.1"}, "bad host"),
        ({"host": "\u212a.example.com"}, "bad host"),  # lowercases to ASCII "k"
        ({"field_origins": {"number": "https://\u212a.example.com"}}, "bad field_origins"),
        ({"host": None}, "bad host"),
        ({"host": "evilshop.example.com.evil.net"}, "the host is outside the card's merchant"),
        ({"host": "evilexample.com"}, "the host is outside the card's merchant"),
        ({"amount": ""}, "bad amount"),
        ({"amount": "x" * 33}, "bad amount"),
        ({"amount": "12\n00"}, "bad amount"),
        ({"amount": 12}, "bad amount"),
        ({"page_title": None}, "bad page_title"),
        ({"field_origins": {}}, "bad field_origins"),
        ({"field_origins": {"pin": "https://a.example.com"}}, "bad field_origins"),
        ({"field_origins": {"number": "ftp://a.example.com"}}, "bad field_origins"),
        ({"field_origins": {"number": "https://a.example.com/x"}}, "bad field_origins"),
        ({"field_origins": {"number": "https://a.example.com:0"}}, "bad field_origins"),
        ({"field_origins": {"number": "https://u@a.example.com"}}, "bad field_origins"),
        ({"field_origins": {"number": "https://10.0.0.1"}}, "bad field_origins"),
        ({"field_origins": ["https://a.example.com"]}, "bad field_origins"),
        (
            {"amount": " ".join([VISA[i : i + 4] for i in range(0, 16, 4)])},
            "the approval text was refused",
        ),
        ({"page_title": "Pay " + VISA}, "the approval text was refused"),
        (
            {"page_title": "Order " + "-".join(THREEDS[i : i + 4] for i in range(0, 16, 4))},
            "the approval text was refused",
        ),
    ],
)
async def test_a_bad_request_is_refused_before_any_row(ctx, over, reason):
    assert await ctx.request(**over) == {"ok": False, "error": reason}
    rows = await ctx.events()
    assert "release_requested" not in [r["event"] for r in rows]
    assert rows[-1]["event"] == "release_refused"
    assert set(rows[-1]["detail"]) == {"reason", "peer"}
    assert ctx.reg.calls == []


async def test_eight_origins_are_too_many(ctx):
    origins = {k: "https://a.example.com" for k in rl._ORIGIN_KEYS}
    origins["extra"] = "https://a.example.com"
    assert (await ctx.request(field_origins=origins))["error"] == "bad field_origins"


@pytest.mark.parametrize("missing", ["card_ref", "host", "amount", "page_title", "claim_hash"])
async def test_a_missing_field_is_refused(ctx, missing):
    assert (await ctx.request(**{missing: _DROP}))["ok"] is False


@pytest.mark.parametrize(
    ("host", "site"),
    [
        (HOST, HOST),
        ("example.com", "example.com"),
        ("a.b.example.com", "a.b.example.com"),
        ("SHOP.Example.com.", HOST),  # canonicalised like the merchant scope
        ("xn--shp-sna.example.com", "xn--shp-sna.example.com"),
    ],
)
async def test_hosts_in_scope_are_canonicalised(ctx, host, site):
    assert (await ctx.request(host=host))["ok"]
    await _until(lambda: ctx.reg.calls)
    assert ctx.reg.calls[0][0]["Site"] == site


async def test_a_single_use_card_skips_the_host_rule(ctx):
    assert (await ctx.request(card_ref=ctx.refs["single"], host="any.example.org"))["ok"]


async def test_a_single_use_card_with_a_merchant_keeps_its_scope(ctx):
    ref = ctx.refs["both"]
    reply = await ctx.request(card_ref=ref, host="any.example.org")
    assert reply == {"ok": False, "error": "the host is outside the card's merchant"}
    assert (await ctx.request(card_ref=ref, host=HOST))["ok"]


async def test_origins_show_the_scheme_unless_https_and_any_port(ctx):
    origins = {
        "number": "https://js.stripe.com",
        "expiry": "http://shop.genesis.test:8080",
        "cvc": "HTTPS://Pay.Example.com:443",
    }
    assert (await ctx.request(field_origins=origins))["ok"] is False  # scheme is lowercase only
    origins["cvc"] = "https://Pay.Example.com:8443"
    assert (await ctx.request(field_origins=origins))["ok"]
    await _until(lambda: ctx.reg.calls)
    assert ctx.reg.calls[0][0]["Fields"] == (
        "cvc=pay.example.com:8443, expiry=http://shop.genesis.test:8080, number=js.stripe.com"
    )
    row = [e for e in await ctx.events() if e["event"] == "release_requested"][0]["detail"]
    assert row["field_origins"]["expiry"] == "http://shop.genesis.test:8080"


@pytest.mark.parametrize(
    ("title", "shown"),
    [
        ("Pay‮evil", "Payevil"),
        ("Z‍W​J", "ZWJ"),
        ("line\none\ttwo", "line one two"),
        ("", "(no title)"),
        ("   \n​ ", "(no title)"),
        ("x" * 200, "x" * 80),
        ("é", "é"),
    ],
)
async def test_the_title_is_sanitised(ctx, title, shown):
    assert (await ctx.request(page_title=title))["ok"]
    await _until(lambda: ctx.reg.calls)
    assert ctx.reg.calls[0][0]["Page title"] == shown


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("locked", "the vault is locked"),
        ("no_approvals", "approval is unavailable"),
        ("stopped", "the vault service is stopping"),
    ],
)
async def test_a_request_needs_an_unlocked_running_service_with_approvals(ctx, state, reason):
    if state == "locked":
        ctx.svc._identity = None
    elif state == "no_approvals":
        ctx.svc._release._approvals = None
    else:
        ctx.svc._stopped = True
    assert (await ctx.request())["error"] == reason


async def test_a_lost_requested_row_means_no_request(ctx, monkeypatch):
    real = ctx.svc._audit

    async def failing(event, detail=None):
        return False if event == "release_requested" else await real(event, detail)

    monkeypatch.setattr(ctx.svc, "_audit", failing)
    assert await ctx.request() == {"ok": False, "error": "the audit write failed"}
    assert ctx.slot() is None and ctx.reg.calls == []


async def test_a_lock_during_the_requested_row_refuses(ctx, monkeypatch):
    real = ctx.svc._audit

    async def locking(event, detail=None):
        ok = await real(event, detail)
        if event == "release_requested":
            ctx.svc._epoch += 1
        return ok

    monkeypatch.setattr(ctx.svc, "_audit", locking)
    reply = await ctx.request()
    assert reply == {"ok": False, "error": "the vault was locked"}
    assert ctx.slot() is None and ctx.reg.calls == []


async def test_the_notice_runs_as_a_tracked_task(ctx, monkeypatch):
    gate = asyncio.Event()

    async def slow(text):
        await gate.wait()
        ctx.reg.notices.append(text)
        return True

    monkeypatch.setattr(ctx.reg, "notify", slow)
    await ctx.request()
    await ctx.request()
    names = [t.get_name() for t in ctx.svc._tasks]
    assert "payment-release-notify" in names
    gate.set()


# -- the peer check is for release ops only -------------------------------------------


async def test_a_refused_peer_gets_a_fixed_error_and_a_row(ctx, monkeypatch):
    def refuse(pid, **_k):
        raise PeerRefused("python env", {"check": "python env", "python_env": ["PYTHONPATH"]})

    monkeypatch.setattr(ctx.svc, "_release_peer", refuse)
    for op in ("release_request", "release_status", "release_claim", "release_cancel"):
        assert await ctx.ask({"op": op, "nonce": "n"}) == {"ok": False, "error": "peer not allowed"}
    row = (await ctx.events())[-1]
    assert row["event"] == "release_refused"
    assert row["detail"]["reason"] == "peer not allowed"
    assert row["detail"]["peer"]["check"] == "python env"
    assert row["detail"]["peer"]["pid"] == os.getpid()
    # The owner's CLI ops keep the uid-only check.
    assert (await ctx.ask({"op": "status"}))["ok"]
    assert (await ctx.ask({"op": "lock"}))["ok"]


async def test_the_peer_check_gets_the_pid_and_accept_time(ctx, monkeypatch):
    seen = {}

    def record(pid, **kw):
        seen.update(pid=pid, **kw)
        return _me()

    monkeypatch.setattr(ctx.svc, "_release_peer", record)
    await ctx.status("n")
    assert seen["pid"] == os.getpid() and isinstance(seen["accept_ticks"], int)
    assert seen["accept_ticks"] >= start_of(os.getpid())


async def test_an_unknown_release_op_is_a_bad_request(ctx):
    assert await ctx.ask({"op": "release_everything"}) == {"ok": False, "error": "bad request"}


def test_the_default_peer_check_is_bound_to_this_server(tmp_path):
    svc = VaultService(None, vault_path=tmp_path, socket_path=tmp_path / "s")
    assert svc._release_peer.keywords["server_pid"] == os.getpid()
    assert svc._release_peer.keywords["policy"].args == ("--server", "health")


# -- the audit trail ---------------------------------------------------------------------


def test_release_events_are_known_and_not_key_events():
    names = {
        "release_requested",
        "release_granted",
        "release_claimed",
        "release_refused",
        "release_cancelled",
    }
    assert names <= audit.EVENTS
    assert not names & audit.KEY_EVENTS
    assert {"unlock", "lock", "idle_relock"} <= audit.KEY_EVENTS


async def test_release_rows_do_not_hide_an_unlock_left_open(db, tmp_path):
    await audit.record_event(db, "unlock")
    for event in ("release_requested", "release_granted", "release_refused"):
        await audit.record_event(db, event, {"nonce": "n"})
    svc = VaultService(db, vault_path=tmp_path / "v", socket_path=tmp_path / "s")
    await svc._correct_trail()
    assert (await audit.list_recent(db, 1))[0]["event"] == "lock"
    assert (await svc.status())["last_event"]["event"] == "lock"


async def test_start_vault_service_hands_the_registry_to_the_release(db, monkeypatch):
    from genesis.payments import vault_service as vsvc

    monkeypatch.setattr("genesis.db.connection.get_db", AsyncMock(return_value=db))
    monkeypatch.setattr(vsvc.VaultService, "start", AsyncMock())
    registry = ap.PaymentApprovalRegistry()
    service = await vsvc.start_vault_service(MagicMock(), registry)
    assert service._release._approvals is registry


# -- vault_release_store.open_card / retire_released ------------------------------------------


def _reseal(path, ref, secret: dict) -> None:
    recipient = x25519.Recipient.from_str((path / vs.RECIPIENT).read_text().strip())
    blob = encrypt(json.dumps(secret).encode(), [recipient])
    with sqlite3.connect(path / vs.CARDS) as conn:
        conn.execute("UPDATE cards SET blob = ? WHERE card_ref = ?", (blob, ref))


def _blob(ref, **over) -> dict:
    secret = {
        "v": 1,
        "card_ref": ref,
        "merchant": "example.com",
        "single_use": False,
        "number": VISA,
        "exp_month": 12,
        "exp_year": _exp_year(),
        "cvc": CVC,
        "name": NAME,
        "postal_code": POSTAL,
    }
    secret.update(over)
    return {k: v for k, v in secret.items() if v is not _DROP}


def test_open_card_returns_the_fields_and_the_row_names(vault, _sealed):
    path, refs = vault
    card = vrs.open_card(refs["scoped"], x25519.Identity.from_str(_sealed[1]), path)
    assert (card["number"], card["label"], card["last4"]) == (VISA, "Test card", "4242")


@pytest.mark.parametrize(
    "over",
    [
        {"v": 2},
        {"v": True},
        {"v": "1"},
        {"extra": "x"},
        {"cvc": _DROP},
        {"exp_month": "12"},
        {"exp_month": 0},
        {"exp_month": 13},
        {"single_use": 0},
        {"single_use": True},
        {"merchant": "other.example"},
        {"card_ref": "card_000000000000"},
    ],
)
def test_open_card_refuses_a_damaged_or_foreign_blob(vault, _sealed, over):
    path, refs = vault
    _reseal(path, refs["scoped"], _blob(refs["scoped"], **over))
    with pytest.raises(vs.VaultError) as info:
        vrs.open_card(refs["scoped"], x25519.Identity.from_str(_sealed[1]), path)
    assert VISA not in str(info.value) and CVC not in str(info.value)
    assert info.value.__cause__ is None and info.value.__context__ is None


def test_open_card_refuses_a_blob_copied_from_another_row(vault, _sealed):
    path, refs = vault
    with sqlite3.connect(path / vs.CARDS) as conn:
        conn.execute(
            "UPDATE cards SET blob = (SELECT blob FROM cards WHERE card_ref = ?)"
            " WHERE card_ref = ?",
            (refs["single"], refs["scoped"]),
        )
    with pytest.raises(vs.VaultError, match="damaged or not this row"):
        vrs.open_card(refs["scoped"], x25519.Identity.from_str(_sealed[1]), path)


def test_open_card_refuses_a_last4_that_is_not_the_sealed_number(vault, _sealed):
    path, refs = vault
    with sqlite3.connect(path / vs.CARDS) as conn:
        conn.execute("UPDATE cards SET last4 = '0000' WHERE card_ref = ?", (refs["scoped"],))
    with pytest.raises(vs.VaultError, match="damaged or not this row") as info:
        vrs.open_card(refs["scoped"], x25519.Identity.from_str(_sealed[1]), path)
    assert info.value.__cause__ is None and info.value.__context__ is None


def test_open_card_with_the_wrong_key_says_nothing_of_the_card(vault):
    path, refs = vault
    with pytest.raises(vs.VaultError) as info:
        vrs.open_card(refs["scoped"], x25519.Identity.generate(), path)
    assert str(info.value) == "the card could not be decrypted with the held key"
    assert info.value.__context__ is None


def test_open_card_refuses_bad_refs_and_retired_cards(vault, _sealed):
    path, refs = vault
    key = x25519.Identity.from_str(_sealed[1])
    with pytest.raises(vs.VaultError, match="not a card ref"):
        vrs.open_card("card_zz", key, path)
    vrs.retire_released(refs["single"], path)
    with pytest.raises(vs.VaultError, match="no active card"):
        vrs.open_card(refs["single"], key, path)


def test_a_lock_with_no_tries_is_refused(vault):
    with pytest.raises(ValueError), vs._dir_lock(vault[0], tries=0):
        pass


def test_retire_released_gives_up_on_a_held_lock(vault, monkeypatch):
    import fcntl

    path, refs = vault
    sleeps = []
    monkeypatch.setattr(vs.time, "sleep", sleeps.append)
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(vs.VaultError, match="busy"):
            vrs.retire_released(refs["single"], path)
    finally:
        os.close(fd)
    assert len(sleeps) == 49
    vrs.retire_released(refs["single"], path)
    assert [c["status"] for c in vs.list_cards(path) if c["card_ref"] == refs["single"]] == [
        "retired"
    ]
