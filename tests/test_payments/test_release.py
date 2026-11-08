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
import secrets
import shutil
import sqlite3
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import aiosqlite
import pytest
from pyrage import encrypt, x25519

from genesis.db.crud import payment_vault_events as audit
from genesis.payments import approval as ap
from genesis.payments import release as rl
from genesis.payments import vault_migrate as vm
from genesis.payments import vault_release_store as vrs
from genesis.payments import vault_store as vs
from genesis.payments.peer_check import FOREGROUND, LAUNCHED, Peer, PeerRefused, start_of
from genesis.payments.vault_service import VaultService
from genesis.security.card_shapes import contains_card_number

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
AMOUNT, TITLE, BUTTON = "12.00 USD", "Checkout", "Pay 12.00 USD"
ALSO = ["name", "email"]  # out of the allowlist's order: the tap shows it in that order


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
def _keyed(tmp_path_factory):
    """A key-file vault (identity.key) and its key text."""
    path = tmp_path_factory.mktemp("keyed") / "payment_vault"
    vs.init_vault(path)
    return path, (path / vs.IDENTITY).read_text().strip()


@pytest.fixture
def vault(_keyed, tmp_path):
    path = tmp_path / "payment_vault"
    shutil.copytree(_keyed[0], path)
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
            "page_title": TITLE,
            "amount": AMOUNT,
            "claim_hash": CLAIM_HASH,
            "field_origins": ORIGINS,
            "button_label": BUTTON,
            "also_filled": ALSO,
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


STALE = "the held key no longer matches recipient.txt"


def _replace_recipient(path) -> None:
    """recipient.txt now names another key: the held one is stale."""
    (path / vs.RECIPIENT).write_text(f"{x25519.Identity.generate().to_public()}\n")


@pytest.fixture
async def ctx(db, vault, tmp_path, caplog):
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
    await svc.start()  # loads identity.key
    assert svc.unlocked
    c = Ctx(svc, reg, refs, clock, db)
    holder["ctx"] = c
    yield c
    await svc.stop()
    await _sweep(c, caplog.text)


async def _sweep(c: Ctx, log_text: str) -> None:
    """No card value in any row, log line, non-claim reply, tap field or notice.

    Rows also never hold the request's free text (amount, title, button label).
    """
    year = str(_exp_year())
    texts = [json.dumps(e) for e in await c.events()]
    for text in texts:
        assert AMOUNT not in text and BUTTON not in text and TITLE not in text, text[:200]
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
    assert list(fields) == [
        "Site",
        "Amount",
        "Card",
        "Requester",
        "Page title",
        "Button",
        "Fields",
        "Also filled",
    ]
    assert fields["Site"] == HOST
    assert fields["Amount"] == "12.00 USD (as stated by Genesis)"
    assert fields["Card"] == "Test card ending 4242"
    assert fields["Requester"] == (
        f"python pid {os.getpid()}, foreground session (claude pid 4321)"
    )
    assert fields["Page title"] == TITLE
    assert fields["Button"] == BUTTON
    assert fields["Fields"] == "cvc=js.stripe.com, number=js.stripe.com"
    assert fields["Also filled"] == "email, name"
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
    reasons = [(e["detail"] or {}).get("reason") for e in await ctx.events()]
    assert "requester gone" in reasons


@pytest.mark.parametrize("granted", [False, True])
async def test_a_dropped_key_refuses_the_release(ctx, granted):
    nonce = await ctx.granted() if granted else (await ctx.request())["nonce"]
    _replace_recipient(ctx.svc._vault)
    await ctx.ask({"op": "status"})  # finds the held key stale and drops it
    assert not ctx.svc.unlocked
    assert (await ctx.status(nonce))["state"] == "refused"
    assert (await ctx.claim(nonce))["error"] == "no such release"
    events = await ctx.events()
    reasons = [e["detail"].get("reason") for e in events if e["event"] == "release_refused"]
    assert STALE in reasons
    dropped = [(e["detail"] or {}).get("reason") for e in events if e["event"] == "key_dropped"]
    assert dropped == [STALE]


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


async def test_a_grant_landing_after_a_key_drop_is_refused(ctx, monkeypatch):
    nonce = (await ctx.request())["nonce"]
    await _until(lambda: ctx.reg.futures)
    ctx.svc._epoch += 1  # a key drop whose abort has not yet run
    ctx.reg.futures[0].set_result(ap.GRANTED)
    await _until(lambda: ctx.slot() is None)
    assert "release_granted" not in await ctx.names()
    assert (await ctx.status(nonce))["state"] == "refused"


async def test_a_lost_grant_row_refuses(ctx, monkeypatch):
    real = ctx.svc._audit

    async def failing(event, detail=None, **kw):
        return False if event == "release_granted" else await real(event, detail, **kw)

    monkeypatch.setattr(ctx.svc, "_audit", failing)
    nonce = (await ctx.request())["nonce"]
    await _until(lambda: ctx.reg.futures)
    ctx.reg.futures[0].set_result(ap.GRANTED)
    await _until(lambda: ctx.slot() is None)
    assert (await ctx.status(nonce))["state"] == "refused"


async def test_stop_refuses_a_pending_release(db, vault, tmp_path):
    reg = FakeRegistry()
    svc = VaultService(
        db,
        vault_path=vault[0],
        socket_path=tmp_path / "s" / "v.sock",
        approvals=reg,
        release_peer=lambda pid, **_k: _me(),
    )
    await svc.start()
    c = Ctx(svc, reg, vault[1], [0.0], db)
    nonce = (await c.request())["nonce"]
    await _until(lambda: reg.futures)
    await svc.stop()
    assert svc._release._recent[nonce] == rl.REFUSED
    assert reg.futures[0].cancelled()
    names = await c.names()
    assert names[-2:] == ["release_refused", "service_stop"]


async def test_only_the_stop_abort_row_passes_the_stopped_guard(db, vault, tmp_path):
    """A release write racing stop()'s abort row is still refused once stopped."""
    reg = FakeRegistry()
    svc = VaultService(
        db,
        vault_path=vault[0],
        socket_path=tmp_path / "s" / "v.sock",
        approvals=reg,
        release_peer=lambda pid, **_k: _me(),
    )
    await svc.start()
    c = Ctx(svc, reg, vault[1], [0.0], db)
    await c.request()
    await _until(lambda: reg.futures)
    real, raced = svc._audit, []

    async def audit(event, detail=None, **kw):
        if kw.get("_stopping") and event == "release_refused" and not raced:
            raced.append(await svc._release._row("release_cancelled", {"nonce": "x"}))
        return await real(event, detail, **kw)

    svc._audit = audit
    await svc.stop()
    assert raced == [False]
    names = await c.names()
    assert "release_cancelled" not in names and names[-2:] == ["release_refused", "service_stop"]


# -- GRANTED ------------------------------------------------------------------------


async def test_a_granted_claim_returns_the_card_once(ctx):
    every = ("number", "expiry", "cvc", "name", "postal_code")
    nonce = await ctx.granted(field_origins={k: "https://js.stripe.com" for k in every})
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

    async def failing(event, detail=None, **kw):
        return False if event == "release_claimed" else await real(event, detail, **kw)

    monkeypatch.setattr(ctx.svc, "_audit", failing)
    assert await ctx.claim(nonce) == {"ok": False, "error": "the audit write failed"}


async def test_a_card_retired_by_the_cli_meanwhile_is_not_released(ctx):
    nonce = await ctx.granted()
    vs.retire_card(ctx.refs["scoped"], ctx.svc._vault)
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


async def test_a_key_drop_before_the_decision_point_aborts_the_claim(ctx, gate):
    entered, release = gate
    nonce = await ctx.granted()
    claim = asyncio.create_task(ctx.claim(nonce))
    await _until(entered.is_set)
    recipient = (ctx.svc._vault / vs.RECIPIENT).read_bytes()
    _replace_recipient(ctx.svc._vault)
    await ctx.ask({"op": "status"})  # drops the stale key
    assert not ctx.svc.unlocked
    # Put it back, so the card opens and the refusal is the drop's alone.
    (ctx.svc._vault / vs.RECIPIENT).write_bytes(recipient)
    release.set()
    assert (await claim) == {"ok": False, "error": "the release was refused"}
    assert "release_claimed" not in await ctx.names()


async def test_a_key_drop_after_the_decision_point_does_not_take_values_back(ctx, monkeypatch):
    nonce = await ctx.granted()
    real = ctx.svc._audit
    entered, release = asyncio.Event(), asyncio.Event()

    async def held(event, detail=None, **kw):
        if event == "release_claimed":
            entered.set()
            await release.wait()
        return await real(event, detail, **kw)

    monkeypatch.setattr(ctx.svc, "_audit", held)
    claim = asyncio.create_task(ctx.claim(nonce))
    await asyncio.wait_for(entered.wait(), 3)
    _replace_recipient(ctx.svc._vault)
    drop = asyncio.create_task(ctx.svc.ensure_key())
    await asyncio.sleep(0.02)
    release.set()
    await drop
    assert not ctx.svc.unlocked
    assert (await claim)["ok"]
    assert "release_claimed" in await ctx.names()
    assert STALE not in [
        (e["detail"] or {}).get("reason")
        for e in await ctx.events()
        if e["event"] == "release_refused"
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

    async def held(event, detail=None, **kw):
        if event == "release_claimed":
            entered.set()
            await release.wait()
        return await real(event, detail, **kw)

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

    async def held(event, detail=None, **kw):
        if event == "release_claimed":
            monkeypatch.setattr(ctx.svc._release, "_alive", lambda pid, start: False)
        return await real(event, detail, **kw)

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
        ({"button_label": 5}, "bad button_label"),
        ({"button_label": ["Pay"]}, "bad button_label"),
        ({"button_label": "(not given)"}, "bad button_label"),  # posing as an absent label
        # ... with an invisible mark the sanitiser used to keep (Mn default-ignorables)
        ({"button_label": "(not given)️"}, "bad button_label"),
        ({"button_label": "(not͏ given)"}, "bad button_label"),
        ({"button_label": "\U000e0041(not given)"}, "bad button_label"),
        ({"button_label": " (not​ given) "}, "bad button_label"),
        ({"also_filled": "email"}, "bad also_filled"),
        ({"also_filled": ["password"]}, "bad also_filled"),
        ({"also_filled": ["Email"]}, "bad also_filled"),
        ({"also_filled": ["email", "email"]}, "bad also_filled"),
        ({"also_filled": [5]}, "bad also_filled"),
        ({"also_filled": {"email": "x"}}, "bad also_filled"),
        ({"also_filled": ["cvc"]}, "bad also_filled"),
        ({"also_filled": ["email", VISA]}, "bad also_filled"),
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
        ({"button_label": "Pay " + VISA}, "the approval text was refused"),
        (
            {"button_label": " ".join(THREEDS[i : i + 4] for i in range(0, 16, 4))},
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


async def test_the_amount_and_title_reach_the_rows_only_as_lengths(ctx):
    short = "4321"  # a CVC-shaped value, made per run; never one a row holds anyway
    while short in {"4321", str(os.getpid())}:  # claude_pid and pid are in every row
        short = str(secrets.randbelow(9000) + 1000)
    amount, title, button = f"{short} USD", f"Pay {short} now", f"Pay {short}"
    nonce = await ctx.granted(amount=amount, page_title=title, button_label=button)
    assert ctx.reg.calls[-1][0]["Amount"].startswith(amount)  # the tap is unchanged
    assert ctx.reg.calls[-1][0]["Page title"] == title
    assert ctx.reg.calls[-1][0]["Button"] == button
    assert (await ctx.claim(nonce))["ok"]
    bounded = re.compile(rf"(?<![0-9A-Za-z_-]){short}(?![0-9A-Za-z_-])")
    for event in await ctx.events():
        assert not bounded.search(json.dumps(event)), event["event"]
    row = [e for e in await ctx.events() if e["event"] == "release_requested"][0]["detail"]
    assert row["field_lengths"] == {
        "amount": len(amount),
        "page_title": len(title),
        "button_label": len(button),
    }
    assert "amount" not in row and "page_title" not in row and "button_label" not in row
    assert row["also_filled"] == ["email", "name"]  # names only, never values
    assert (row["host"], row["card_ref"]) == (HOST, ctx.refs["scoped"])
    assert row["field_origins"] == {"cvc": "js.stripe.com", "number": "js.stripe.com"}


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
    ("label", "shown"),
    [
        ("Pay $999.00", "Pay $999.00"),
        ("Pay\u202e999", "Pay999"),
        ("Sub\u200dscr\u200bibe", "Subscribe"),
        ("Pay\n now\t", "Pay now"),
        ("", "(no label)"),
        ("  \n\u200b ", "(no label)"),
        ("Pay now \x85x", "Pay now x"),  # line and paragraph separators, NEL
        ("x" * 200, "x" * 40),
        ("x" * 41, "x" * 40),
        ("x" * 40, "x" * 40),
        ("x" * 39 + " yz", "x" * 39),  # a cut that ends on a space drops it
        ("Payer \u00e9", "Payer \u00e9"),
    ],
)
async def test_the_button_label_is_sanitised_and_capped(ctx, label, shown):
    assert rl._BUTTON_CHARS == 40
    assert (await ctx.request(button_label=label))["ok"]
    await _until(lambda: ctx.reg.calls)
    assert ctx.reg.calls[0][0]["Button"] == shown
    row = [e for e in await ctx.events() if e["event"] == "release_requested"][0]["detail"]
    assert row["field_lengths"]["button_label"] == len(shown)


@pytest.mark.parametrize("absent", [_DROP, None])
async def test_a_button_label_not_given_says_so_in_the_tap(ctx, absent):
    assert (await ctx.request(button_label=absent))["ok"]
    await _until(lambda: ctx.reg.calls)
    assert ctx.reg.calls[0][0]["Button"] == "(not given)"
    row = [e for e in await ctx.events() if e["event"] == "release_requested"][0]["detail"]
    assert row["field_lengths"]["button_label"] is None


@pytest.mark.parametrize(
    ("names", "shown"),
    [
        (_DROP, "none"),
        (None, "none"),
        ([], "none"),
        (["country", "email"], "email, country"),
        (
            ["company", "country", "postal_code", "region", "city"]
            + ["address_line2", "address_line1", "phone", "name", "email"],
            "email, name, phone, address_line1, address_line2, city, region,"
            " postal_code, country, company",
        ),
    ],
)
async def test_the_also_filled_line_names_the_extra_fields(ctx, names, shown):
    assert rl.FILL_NAMES == (
        "email",
        "name",
        "phone",
        "address_line1",
        "address_line2",
        "city",
        "region",
        "postal_code",
        "country",
        "company",
    )
    assert (await ctx.request(also_filled=names))["ok"]
    await _until(lambda: ctx.reg.calls)
    assert ctx.reg.calls[0][0]["Also filled"] == shown
    row = [e for e in await ctx.events() if e["event"] == "release_requested"][0]["detail"]
    assert row["also_filled"] == ([] if shown == "none" else shown.split(", "))


@pytest.mark.parametrize(
    ("state", "reason"),
    [
        ("not_loaded", "the vault key is not loaded (see vault status)"),
        ("no_approvals", "approval is unavailable"),
        ("stopped", "the vault service is stopping"),
    ],
)
async def test_a_request_needs_a_loaded_key_a_running_service_and_approvals(
    ctx, monkeypatch, state, reason
):
    if state == "not_loaded":
        ctx.svc._identity = None

        def refused(_path=None):
            raise vs.VaultError("the key cannot be loaded (test)")

        monkeypatch.setattr(vrs, "load_identity", refused)
    elif state == "no_approvals":
        ctx.svc._release._approvals = None
    else:
        ctx.svc._stopped = True
    assert (await ctx.request())["error"] == reason


async def test_a_request_loads_a_key_not_held(ctx):
    """A key loaded since start (a vault migrated under a running server) is picked up."""
    ctx.svc._identity = None
    reply = await ctx.request()
    assert reply["ok"] and ctx.svc.unlocked, reply
    names = await ctx.names()
    assert names.count("key_loaded") == 2, names  # start, then the request


async def test_a_lost_requested_row_means_no_request(ctx, monkeypatch):
    real = ctx.svc._audit

    async def failing(event, detail=None, **kw):
        return False if event == "release_requested" else await real(event, detail, **kw)

    monkeypatch.setattr(ctx.svc, "_audit", failing)
    assert await ctx.request() == {"ok": False, "error": "the audit write failed"}
    assert ctx.slot() is None and ctx.reg.calls == []


async def test_a_key_drop_during_the_requested_row_refuses(ctx, monkeypatch):
    real = ctx.svc._audit

    async def locking(event, detail=None, **kw):
        ok = await real(event, detail, **kw)
        if event == "release_requested":
            ctx.svc._epoch += 1
        return ok

    monkeypatch.setattr(ctx.svc, "_audit", locking)
    reply = await ctx.request()
    assert reply == {"ok": False, "error": "the vault key was dropped"}
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
    # The retired lock op is answered as retired, not as a refused peer.
    assert await ctx.ask({"op": "lock"}) == {"ok": False, "error": vm.RETIRED}


def _long_digits() -> str:
    """A 16-digit run made per run (never a fixture literal)."""
    return str(secrets.randbelow(9 * 10**15) + 10**15)


async def test_a_refused_peer_with_a_long_digit_run_is_withheld_from_the_row(ctx, monkeypatch):
    digits = _long_digits()

    def refuse(pid, **_k):
        detail = {"check": "exe mismatch", "exe": f"/tmp/{digits}", "python_env": [f"X{digits}"]}
        raise PeerRefused("exe mismatch", detail)

    monkeypatch.setattr(ctx.svc, "_release_peer", refuse)
    assert (await ctx.ask({"op": "release_status", "nonce": "n"}))["error"] == "peer not allowed"
    row = (await ctx.events())[-1]
    assert digits not in json.dumps(row)
    assert row["detail"]["peer"]["exe"] == row["detail"]["peer"]["python_env"][0]
    assert row["detail"]["peer"]["check"] == "exe mismatch"  # the rest is kept


async def test_an_unmatched_nonce_is_not_recorded(ctx):
    digits = _long_digits()
    assert (await ctx.claim(digits))["error"] == "no such release"
    nonce = await ctx.granted()
    assert (await ctx.claim(nonce))["ok"]
    assert (await ctx.claim(nonce))["error"] == "no such release"  # ended: still recorded
    refused = [e["detail"] for e in await ctx.events() if e["event"] == "release_refused"]
    assert digits not in json.dumps(refused)
    assert [d.get("nonce") for d in refused] == [None, nonce]


@pytest.mark.parametrize(
    ("origins", "keys"),
    [
        ({"postal_code": "https://shop.example.com"}, {"postal_code"}),
        ({"expiry": "https://shop.example.com"}, {"exp_month", "exp_year"}),
        ({"number": "https://a.example.com", "cvc": "https://a.example.com"}, {"number", "cvc"}),
        (
            {"exp_year": "https://a.example.com", "name": "https://a.example.com"},
            {"exp_year", "name"},
        ),
    ],
)
async def test_the_claim_returns_only_the_fields_the_tap_named(ctx, origins, keys):
    nonce = await ctx.granted(field_origins=origins)
    reply = await ctx.claim(nonce)
    assert set(reply["card"]) == keys | {"label", "last4"}
    del reply


async def test_a_slot_ended_before_the_claim_settles_returns_nothing(ctx, monkeypatch):
    nonce = await ctx.granted()
    mgr = ctx.svc._release
    real = mgr._end

    def ended(rel, state):
        if state == rl.CLAIMED:
            real(rel, rl.REFUSED)  # something else settled it first
            return False
        return real(rel, state)

    monkeypatch.setattr(mgr, "_end", ended)
    reply = await ctx.claim(nonce)
    assert reply == {"ok": False, "error": "the release was refused"}


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
    assert {"key_loaded", "key_load_refused", "key_dropped"} <= audit.KEY_EVENTS
    assert {"unlock", "lock", "idle_relock"} <= audit.KEY_EVENTS  # earlier rows stay valid


async def test_release_rows_do_not_hide_the_last_key_event(db, tmp_path, monkeypatch):
    await audit.record_event(db, "key_loaded")
    for event in ("release_requested", "release_granted", "release_refused"):
        await audit.record_event(db, event, {"nonce": "n"})
    svc = VaultService(db, vault_path=tmp_path / "v", socket_path=tmp_path / "s")
    monkeypatch.setattr(svc, "ensure_key", AsyncMock())  # no row of its own
    assert (await audit.list_recent(db, 1))[0]["event"] == "release_refused"
    assert (await svc.status())["last_event"]["event"] == "key_loaded"


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


def test_open_card_returns_the_fields_and_the_row_names(vault, _keyed):
    path, refs = vault
    card = vrs.open_card(refs["scoped"], x25519.Identity.from_str(_keyed[1]), path)
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
def test_open_card_refuses_a_damaged_or_foreign_blob(vault, _keyed, over):
    path, refs = vault
    _reseal(path, refs["scoped"], _blob(refs["scoped"], **over))
    with pytest.raises(vs.VaultError) as info:
        vrs.open_card(refs["scoped"], x25519.Identity.from_str(_keyed[1]), path)
    assert VISA not in str(info.value) and CVC not in str(info.value)
    assert info.value.__cause__ is None and info.value.__context__ is None


def test_open_card_refuses_a_blob_copied_from_another_row(vault, _keyed):
    path, refs = vault
    with sqlite3.connect(path / vs.CARDS) as conn:
        conn.execute(
            "UPDATE cards SET blob = (SELECT blob FROM cards WHERE card_ref = ?)"
            " WHERE card_ref = ?",
            (refs["single"], refs["scoped"]),
        )
    with pytest.raises(vs.VaultError, match="damaged or not this row"):
        vrs.open_card(refs["scoped"], x25519.Identity.from_str(_keyed[1]), path)


def test_open_card_refuses_a_last4_that_is_not_the_sealed_number(vault, _keyed):
    path, refs = vault
    with sqlite3.connect(path / vs.CARDS) as conn:
        conn.execute("UPDATE cards SET last4 = '0000' WHERE card_ref = ?", (refs["scoped"],))
    with pytest.raises(vs.VaultError, match="damaged or not this row") as info:
        vrs.open_card(refs["scoped"], x25519.Identity.from_str(_keyed[1]), path)
    assert info.value.__cause__ is None and info.value.__context__ is None


def test_open_card_with_the_wrong_key_says_nothing_of_the_card(vault):
    path, refs = vault
    with pytest.raises(vs.VaultError) as info:
        vrs.open_card(refs["scoped"], x25519.Identity.generate(), path)
    assert str(info.value) == "the card could not be decrypted with the held key"
    assert info.value.__context__ is None


def test_open_card_refuses_bad_refs_and_retired_cards(vault, _keyed):
    path, refs = vault
    key = x25519.Identity.from_str(_keyed[1])
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


async def test_split_name_kinds_are_tapped_and_claimed_as_the_sealed_name(ctx):
    origins = {
        "number": "https://js.stripe.com",
        "first_name": "https://js.stripe.com",
        "last_name": "https://js.stripe.com",
    }
    nonce = await ctx.granted(field_origins=origins)
    fields = ctx.reg.calls[-1][0]
    assert "first_name=js.stripe.com" in fields["Fields"]
    assert "last_name=js.stripe.com" in fields["Fields"]
    reply = await ctx.claim(nonce)
    assert reply["ok"] and set(reply["card"]) == {"number", "name", "label", "last4"}
    assert reply["card"]["name"] == NAME


def _add(ctx, *, name, single_use=False):
    card = vs.parse_card(VISA, f"12/{_exp_year()}", CVC)
    return vs.add_card(
        card,
        label="Name test card",
        merchant=None if single_use else "example.com",
        single_use=single_use,
        name=name,
        postal_code=POSTAL,
        path=ctx.svc._vault,
    )


@pytest.mark.parametrize(
    ("name", "kinds"),
    [("", ("name",)), ("", ("first_name", "last_name")), ("Single", ("first_name", "last_name"))],
)
async def test_a_card_with_no_usable_name_is_refused_unspent(ctx, name, kinds):
    ref = _add(ctx, name=name, single_use=True)
    origins = {"number": "https://js.stripe.com", **{k: "https://js.stripe.com" for k in kinds}}
    nonce = await ctx.granted(card_ref=ref, field_origins=origins)
    reply = await ctx.claim(nonce)
    assert reply == {"ok": False, "error": "the card has no usable name for the name fields"}
    # A single-use card stays usable: nothing was retired.
    assert (
        next(c for c in vs.list_cards(ctx.svc._vault) if c["card_ref"] == ref)["status"] == "active"
    )


async def test_a_one_word_name_is_fine_for_the_whole_name_kind(ctx):
    ref = _add(ctx, name="Single")
    nonce = await ctx.granted(
        card_ref=ref,
        field_origins={"number": "https://js.stripe.com", "name": "https://x.example.com"},
    )
    assert (await ctx.claim(nonce))["ok"]


async def test_consent_labels_are_shown_and_recorded_as_lengths(ctx):
    label = "I authorize this debit"
    reply = await ctx.request(consent_labels=[label])
    assert reply["shown"] == ["consent"]
    nonce = reply["nonce"]
    fields = ctx.reg.calls[-1][0]
    assert fields["Will tick"] == f'"{label}"'
    row = next(e for e in await ctx.events() if e["event"] == "release_requested")
    assert row["detail"]["field_lengths"]["consent_labels"] == [len(label)]
    assert label not in json.dumps(row)
    assert nonce


async def test_no_consent_labels_leave_the_tap_unchanged(ctx):
    assert (await ctx.request())["shown"] == []
    assert "Will tick" not in ctx.reg.calls[-1][0]


@pytest.mark.parametrize("value", ["one", [], ["a", "b", "c"], [3], ["  "], ["x" * 601]])
async def test_bad_consent_labels_are_refused(ctx, value):
    assert await ctx.request(consent_labels=value) == {"ok": False, "error": "bad consent_labels"}


async def test_a_ceiling_and_a_save_button_are_shown_and_echoed(ctx):
    reply = await ctx.request(amount_max="30.00 USD", save_label="Add Card")
    assert set(reply["shown"]) == {"amount_max", "save"}
    fields = ctx.reg.calls[-1][0]
    assert fields["Amount"].startswith("up to 30.00 USD (subtotal 12.00 USD")
    assert fields["Save button"] == "Add Card"
    row = next(e for e in await ctx.events() if e["event"] == "release_requested")
    assert row["detail"]["field_lengths"]["amount_max"] == len("30.00 USD")


@pytest.mark.parametrize("value", ["", 5, "x" * 33])
async def test_a_bad_ceiling_is_refused(ctx, value):
    assert await ctx.request(amount_max=value) == {"ok": False, "error": "bad amount_max"}

