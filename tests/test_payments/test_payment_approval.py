"""Card-release approval: the registry, the Telegram tap through the real handler, and its registration."""

from __future__ import annotations

import asyncio
import importlib
import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import aiosqlite
import pytest
from flask import Flask
from telegram import CallbackQuery, Chat, InaccessibleMessage, Message, Update, User

# Importing the route modules registers them on the shared blueprint, which must
# happen before any app registers it.
import genesis.dashboard.routes.comms  # noqa: F401
import genesis.dashboard.routes.state  # noqa: F401
from genesis.channels.telegram._handler_context import HandlerContext
from genesis.channels.telegram._handler_messages import handle_callback_query
from genesis.dashboard._blueprint import blueprint
from genesis.db.crud import payment_vault_events as audit
from genesis.payments import approval as ap
from genesis.payments.approval import PAYMENT_APPROVAL_ACTION_TYPE, PaymentApprovalRegistry
from genesis.payments.vault_service import VaultService

MIG = importlib.import_module("genesis.db.migrations.20261011060631_payment_vault_events")
OWNER, OTHER, STRANGER = 1001, 1002, 1003
FIELDS = {"Merchant": "example.test", "Amount": "12.00 USD"}
BOT_USER = User(id=9, first_name="bot", is_bot=True)


class FakeBot:
    """The Bot methods this path calls, returning real telegram objects."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.sent: list[Message] = []
        self.answers: list[dict] = []
        self.markup_edits: list[dict] = []

    async def send_message(self, **kw):
        if self.fail:
            raise RuntimeError("network down")
        assert "parse_mode" not in kw, "approval text must be sent with no parse mode"
        msg = Message(
            message_id=77 + len(self.sent),
            date=datetime.now(UTC),
            chat=Chat(id=kw["chat_id"], type=Chat.PRIVATE),
            text=kw["text"],
            from_user=BOT_USER,
            reply_markup=kw.get("reply_markup"),
        )
        msg.set_bot(self)
        self.sent.append(msg)
        return msg

    async def answer_callback_query(self, **kw):
        self.answers.append(kw)
        return True

    async def edit_message_reply_markup(self, **kw):
        self.markup_edits.append(kw)
        return True


@pytest.fixture
async def db(tmp_path):
    async with aiosqlite.connect(str(tmp_path / "g.db")) as conn:
        await MIG.up(conn)
        yield conn


@pytest.fixture
def make(db, tmp_path):
    """A registry auditing through a real VaultService connection, with a fake bot."""

    def _make(*, timeout_s: float = 60.0, bot: FakeBot | None = None, attach: bool = True):
        registry = PaymentApprovalRegistry(timeout_s=timeout_s)
        svc = VaultService(db, vault_path=tmp_path / "v", socket_path=tmp_path / "run" / "s")
        registry.attach_audit(svc._audit)
        bot = bot or FakeBot()
        if attach:
            registry.attach_bot(bot, OWNER)
        ctx = HandlerContext(
            loop=MagicMock(),
            allowed_users={OWNER, OTHER},
            whisper_model="none",
            payment_approvals=registry,
        )
        return registry, bot, ctx

    return _make


async def _wait_recorded(registry) -> str:
    """The nonce, once the send has returned and the record holds the message."""
    for _ in range(500):  # the audit row is written off the loop (aiosqlite), so poll
        for nonce, pending in registry._pending.items():
            if pending.message_id is not None:
                return nonce
        await asyncio.sleep(0.01)
    raise AssertionError("the request never recorded its message")


def _msg(sent: Message, bot, **over) -> Message:
    fields = {
        "message_id": sent.message_id,
        "date": sent.date,
        "chat": sent.chat,
        "text": sent.text,
        "edit_date": None,
    } | over
    msg = Message(**fields)
    msg.set_bot(bot)
    return msg


async def _tap(ctx, bot, *, data: str, user_id: int = OWNER, message=None) -> dict:
    query = CallbackQuery(
        id=f"q{len(bot.answers)}",
        from_user=User(id=user_id, first_name="u", is_bot=False),
        chat_instance="ci",
        data=data,
        message=message,
    )
    query.set_bot(bot)
    await handle_callback_query(ctx, Update(update_id=1, callback_query=query), MagicMock())
    return bot.answers[-1]


async def _events(db) -> list[str]:
    return [r["event"] for r in reversed(await audit.list_recent(db, 100))]


# -- the tap, through the real callback handler --------------------------------


async def test_owner_tap_on_the_sent_message_grants(make, db):
    registry, bot, ctx = make()
    task = asyncio.create_task(registry.request(FIELDS))
    nonce = await _wait_recorded(registry)
    sent = bot.sent[0]
    assert sent.chat.id == OWNER and "example.test" in sent.text
    button = sent.reply_markup.inline_keyboard[0][0]
    assert button.callback_data == f"payapprove:{nonce}"
    answer = await _tap(ctx, bot, data=button.callback_data, message=_msg(sent, bot))
    assert answer["text"] == "Approved"
    assert await task == ap.GRANTED
    assert bot.markup_edits, "the button is removed after a grant"
    assert await _events(db) == ["approval_requested", "approval_granted"]


@pytest.mark.parametrize(
    "case",
    [
        "other_user",
        "stranger",
        "other_chat",
        "other_message",
        "edited",
        "changed_text",
        "inaccessible",
        "no_message",
    ],
)
async def test_every_mismatch_refuses(make, db, case):
    registry, bot, ctx = make()
    task = asyncio.create_task(registry.request(FIELDS))
    nonce = await _wait_recorded(registry)
    sent = bot.sent[0]
    user, message = OWNER, _msg(sent, bot)
    if case == "other_user":
        user = OTHER  # on the allowlist, but not its first id
    elif case == "stranger":
        user = STRANGER
    elif case == "other_chat":
        message = _msg(sent, bot, chat=Chat(id=OTHER, type=Chat.PRIVATE))
    elif case == "other_message":
        message = _msg(sent, bot, message_id=sent.message_id + 1)
    elif case == "edited":
        message = _msg(sent, bot, edit_date=datetime.now(UTC))
    elif case == "changed_text":
        message = _msg(sent, bot, text=sent.text + " ")
    elif case == "inaccessible":
        message = InaccessibleMessage(chat=sent.chat, message_id=sent.message_id)
    elif case == "no_message":
        message = None
    answer = await _tap(ctx, bot, data=f"payapprove:{nonce}", user_id=user, message=message)
    assert answer["text"] == "Refused"
    assert await task == ap.REFUSED
    assert not bot.markup_edits
    assert await _events(db) == ["approval_requested", "approval_refused"]


async def test_unknown_nonce_refuses_and_leaves_the_request_open(make):
    registry, bot, ctx = make(timeout_s=0.2)
    task = asyncio.create_task(registry.request(FIELDS))
    await _wait_recorded(registry)
    answer = await _tap(ctx, bot, data="payapprove:not-a-nonce", message=_msg(bot.sent[0], bot))
    assert answer["text"] == "Refused"
    assert not task.done()
    assert await task == ap.EXPIRED


async def test_a_second_tap_is_refused(make):
    registry, bot, ctx = make()
    task = asyncio.create_task(registry.request(FIELDS))
    nonce = await _wait_recorded(registry)
    message = _msg(bot.sent[0], bot)
    first = await _tap(ctx, bot, data=f"payapprove:{nonce}", message=message)
    second = await _tap(ctx, bot, data=f"payapprove:{nonce}", message=message)
    assert (first["text"], second["text"]) == ("Approved", "Refused")
    assert await task == ap.GRANTED


async def test_two_concurrent_taps_grant_once(make, db):
    """Both taps arrive while the grant's audit row is being written: one grant only."""
    registry, bot, ctx = make()
    task = asyncio.create_task(registry.request(FIELDS))
    nonce = await _wait_recorded(registry)
    message = _msg(bot.sent[0], bot)
    results = await asyncio.gather(
        registry.handle_tap(nonce, OWNER, message), registry.handle_tap(nonce, OWNER, message)
    )
    assert sorted(results) == [False, True]
    assert await task == ap.GRANTED
    assert (await _events(db)).count("approval_granted") == 1


async def test_no_tap_expires(make, db):
    registry, bot, ctx = make(timeout_s=0.05)
    assert await registry.request(FIELDS) == ap.EXPIRED
    assert registry._pending == {}
    assert await _events(db) == ["approval_requested", "approval_expired"]
    # A tap after expiry finds nothing.
    nonce = bot.sent[0].reply_markup.inline_keyboard[0][0].callback_data
    assert (await _tap(ctx, bot, data=nonce, message=_msg(bot.sent[0], bot)))["text"] == "Refused"


async def test_no_bot_attached_refuses_without_sending(make, db):
    registry, bot, ctx = make(attach=False)
    assert await registry.request(FIELDS) == ap.REFUSED
    assert bot.sent == []
    assert await _events(db) == ["approval_refused"]


async def test_legacy_bridge_has_no_registry(make):
    _registry, bot, ctx = make()
    ctx.payment_approvals = None
    answer = await _tap(ctx, bot, data="payapprove:x", message=None)
    assert answer["text"] == "Payment approval is unavailable here"


async def test_default_timeout_is_ten_minutes():
    assert ap.DEFAULT_TIMEOUT_S == 600
    assert PaymentApprovalRegistry()._timeout_s == 600


# -- fail closed ----------------------------------------------------------------


async def test_no_audit_refuses_without_sending():
    registry = PaymentApprovalRegistry()
    bot = FakeBot()
    registry.attach_bot(bot, OWNER)
    assert await registry.request(FIELDS) == ap.REFUSED
    assert bot.sent == []


async def test_a_grant_whose_audit_row_fails_is_refused(make, db, monkeypatch):
    registry, bot, ctx = make()
    task = asyncio.create_task(registry.request(FIELDS))
    nonce = await _wait_recorded(registry)
    real = audit.record_event

    async def no_grant_row(conn, event, detail=None):
        if event == "approval_granted":
            raise OSError("disk full")
        await real(conn, event, detail)

    monkeypatch.setattr(audit, "record_event", no_grant_row)
    answer = await _tap(ctx, bot, data=f"payapprove:{nonce}", message=_msg(bot.sent[0], bot))
    assert answer["text"] == "Refused"
    assert await task == ap.REFUSED


async def test_a_failed_send_refuses(make, db):
    registry, _bot, _ctx = make(bot=FakeBot(fail=True))
    assert await registry.request(FIELDS) == ap.REFUSED
    assert registry._pending == {}
    assert await _events(db) == ["approval_requested", "approval_refused"]


async def test_a_tap_racing_the_send_refuses(make):
    """A tap that lands before the send has returned refuses; it never waits to grant."""
    registry, bot, ctx = make()
    real_send = bot.send_message

    async def send_then_tap(**kw):
        msg = await real_send(**kw)
        nonce = next(iter(registry._pending))
        await _tap(ctx, bot, data=f"payapprove:{nonce}", message=_msg(msg, bot))
        return msg

    bot.send_message = send_then_tap
    assert await registry.request(FIELDS) == ap.REFUSED
    assert bot.answers[-1]["text"] == "Refused"


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"Merchant": "a\nApproved: yes"},
        {"Merchant": ""},
        {"Card": "4242 4242 4242 4242"},  # Stripe's public test number
        {"Note": "x" * 4097},
        {"Note": "\U0001f600" * 2049},  # 4098 UTF-16 code units
    ],
)
async def test_text_it_will_not_send_is_refused(make, fields):
    registry, bot, _ctx = make()
    assert await registry.request(fields) == ap.REFUSED
    assert bot.sent == []


def test_text_is_built_in_code_with_one_line_per_field():
    assert ap.build_text(FIELDS).splitlines()[:3] == [
        "Payment approval",
        "Merchant: example.test",
        "Amount: 12.00 USD",
    ]


# -- owner is the FIRST allowlist id, in written order ---------------------------


def test_allowlist_keeps_written_order_and_its_set():
    from genesis.channels.bridge_config import build_bridge_config

    cfg = build_bridge_config(
        {"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_USERS": "300, 100,x,200,300"}
    )
    assert cfg["allowed_users_ordered"] == (300, 100, 200)
    assert cfg["allowed_users"] == {100, 200, 300}


# -- audit trail interleaving -----------------------------------------------------


async def test_approval_rows_do_not_hide_an_unlock_left_open(db, tmp_path):
    """The trail-correction reads the last KEY event, not the last row."""
    await audit.record_event(db, "unlock")
    await audit.record_event(db, "approval_requested", {"nonce": "n"})
    svc = VaultService(db, vault_path=tmp_path / "v", socket_path=tmp_path / "s")
    await svc._correct_trail()
    assert (await audit.list_recent(db, 1))[0]["event"] == "lock"


async def test_status_last_event_is_the_last_key_event(db, tmp_path):
    await audit.record_event(db, "lock")
    await audit.record_event(db, "approval_requested", {"nonce": "n"})
    svc = VaultService(db, vault_path=tmp_path / "v", socket_path=tmp_path / "s")
    assert (await svc.status())["last_event"]["event"] == "lock"


async def test_start_vault_service_attaches_the_registry_audit(db, monkeypatch):
    from genesis.payments import vault_service as vsvc

    monkeypatch.setattr("genesis.db.connection.get_db", AsyncMock(return_value=db))
    monkeypatch.setattr(vsvc.VaultService, "start", AsyncMock())
    registry = PaymentApprovalRegistry()
    service = await vsvc.start_vault_service(MagicMock(), registry)
    assert registry._audit == service._audit


# -- cancellation and the deadline ---------------------------------------------------


def _gated_audit(svc_audit):
    """The real audit, held at the grant row until ``release`` is set."""
    entered, release = asyncio.Event(), asyncio.Event()

    async def audit_fn(event, detail):
        if event == "approval_granted":
            entered.set()
            await release.wait()
        return await svc_audit(event, detail)

    return audit_fn, entered, release


async def test_a_tap_cancelled_during_its_grant_row_settles_the_request(make):
    registry, bot, _ctx = make(timeout_s=60)
    audit_fn, entered, _release = _gated_audit(registry._audit)
    registry.attach_audit(audit_fn)
    task = asyncio.create_task(registry.request(FIELDS))
    nonce = await _wait_recorded(registry)
    tap = asyncio.create_task(registry.handle_tap(nonce, OWNER, _msg(bot.sent[0], bot)))
    await asyncio.wait_for(entered.wait(), 2)
    tap.cancel()
    assert await asyncio.wait_for(task, 2) == ap.REFUSED  # not the 60 s deadline


async def test_the_deadline_does_not_depend_on_the_pending_dict(make):
    registry, _bot, _ctx = make(timeout_s=0.2)
    task = asyncio.create_task(registry.request(FIELDS))
    await _wait_recorded(registry)
    registry._pending.clear()  # a consumer that took the nonce and never settled it
    assert await asyncio.wait_for(task, 2) == ap.EXPIRED


async def test_a_grant_row_then_expiry_records_the_refusal(make, db):
    registry, bot, _ctx = make(timeout_s=0.2)
    audit_fn, entered, release = _gated_audit(registry._audit)
    registry.attach_audit(audit_fn)
    task = asyncio.create_task(registry.request(FIELDS))
    nonce = await _wait_recorded(registry)
    tap = asyncio.create_task(registry.handle_tap(nonce, OWNER, _msg(bot.sent[0], bot)))
    await asyncio.wait_for(entered.wait(), 2)
    assert await asyncio.wait_for(task, 2) == ap.EXPIRED
    release.set()
    assert await tap is False
    rows = list(reversed(await audit.list_recent(db, 100)))
    assert [r["event"] for r in rows] == [
        "approval_requested",
        "approval_expired",
        "approval_granted",
        "approval_refused",
    ]
    assert rows[-1]["detail"]["reason"] == "requester gone after grant row"


async def test_a_cancelled_requester_gets_no_grant(make, db):
    registry, bot, _ctx = make(timeout_s=60)
    audit_fn, entered, release = _gated_audit(registry._audit)
    registry.attach_audit(audit_fn)
    task = asyncio.create_task(registry.request(FIELDS))
    nonce = await _wait_recorded(registry)
    tap = asyncio.create_task(registry.handle_tap(nonce, OWNER, _msg(bot.sent[0], bot)))
    await asyncio.wait_for(entered.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    release.set()
    assert await tap is False
    assert (await _events(db))[-1] == "approval_refused"


# -- round 1 review: the digit-run guard, value-light rows, the healing send -------


@pytest.mark.parametrize(
    "value",
    [
        "4111\u20131111\u20131111\u20131111",  # en dash
        "4111\u20141111\u20141111\u20141111",  # em dash
        "4242\u00a04242\u00a04242\u00a04242",  # no-break space
        "4222222222222",  # 13-digit Visa, outside the detector's grammar
        "\uff14\uff12\uff14\uff12" * 3,  # fullwidth digits: str.isdigit is True
        "4242 4242 4242 4242",  # Stripe's public test number
        "Order 1234-5678-9012",  # 12 digits in a run: refused on purpose (fails safe)
    ],
)
async def test_a_digit_run_is_refused_in_fields_and_audit_detail(make, db, value):
    assert "\uff14".isdigit()
    registry, bot, _ctx = make(timeout_s=0.05)  # a send that slips through expires fast
    assert await registry.request({"Note": value}) == ap.REFUSED
    assert await registry.request(FIELDS, audit_detail={"page": value}) == ap.REFUSED
    assert bot.sent == []
    assert value not in json.dumps(await audit.list_recent(db, 100), ensure_ascii=False)


@pytest.mark.parametrize("value", ["12.00 USD", "card 4242", "2026-10-18", "ab12345678901cd"])
def test_short_or_letter_broken_digits_pass(value):
    assert not ap.holds_long_digit_run(value)


async def test_the_audit_row_is_value_light(make, db):
    registry, bot, ctx = make(timeout_s=0.05)
    shown = {"Merchant": "example.test", "Page": "Checkout - Example Store"}
    facts = {
        "card_ref": "c-1",
        "host": "example.test",
        "amount": "12.00 USD",
        "last4": "4242",
        "requester": {"pid": 7, "comm": "python"},
    }
    assert await registry.request(shown, audit_detail=facts) == ap.EXPIRED
    row = (await audit.list_recent(db, 100))[-1]
    assert row["event"] == "approval_requested"
    assert "Checkout - Example Store" not in json.dumps(row)
    assert row["detail"]["field_lengths"] == {"Merchant": 12, "Page": 24}
    assert {k: row["detail"][k] for k in facts} == facts


async def test_free_text_is_not_a_plain_audit_value(make):
    registry, _bot, _ctx = make()
    assert await registry.request(FIELDS, audit_detail={"page": ["a", "b"]}) == ap.REFUSED


async def test_the_send_heals_a_closed_client(make):
    bot = FakeBot()
    bot.request = MagicMock(initialize=AsyncMock())
    real_send = bot.send_message
    calls = []

    async def closed_once(**kw):
        calls.append(kw)
        if len(calls) == 1:
            raise RuntimeError("This HTTPXRequest is not initialized!")
        return await real_send(**kw)

    bot.send_message = closed_once
    registry, _bot, _ctx = make(bot=bot, timeout_s=0.05)
    assert await registry.request(FIELDS) == ap.EXPIRED  # sent, then nobody tapped
    bot.request.initialize.assert_awaited_once()
    assert len(bot.sent) == 1


async def test_a_tap_landing_as_the_deadline_fires_wins(make, db, monkeypatch):
    """Deterministic: the future is settled at the moment wait_for reports its timeout."""
    registry, _bot, _ctx = make(timeout_s=60)
    real = asyncio

    class _Asyncio:
        def __getattr__(self, name):
            return getattr(real, name)

        @staticmethod
        async def wait_for(aw, timeout):
            next(iter(registry._pending.values())).future.set_result(ap.GRANTED)
            aw.cancel()
            raise TimeoutError

    monkeypatch.setattr(ap, "asyncio", _Asyncio())
    assert await registry.request(FIELDS) == ap.GRANTED
    assert "approval_expired" not in await _events(db)


async def test_a_cancelled_requester_is_audited_and_its_button_refuses(make, db):
    registry, bot, ctx = make()
    task = asyncio.create_task(registry.request(FIELDS))
    nonce = await _wait_recorded(registry)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    rows = list(reversed(await audit.list_recent(db, 100)))
    assert (rows[-1]["event"], rows[-1]["detail"]["reason"]) == (
        "approval_refused",
        "requester cancelled",
    )
    answer = await _tap(ctx, bot, data=f"payapprove:{nonce}", message=_msg(bot.sent[0], bot))
    assert answer["text"] == "Refused"
    last = (await audit.list_recent(db, 1))[0]
    assert last["detail"]["reason"] == "unknown nonce" and last["detail"]["nonce"] == nonce


# -- production wiring ------------------------------------------------------------


async def test_start_payment_services_builds_the_registry_the_vault_audits(db, monkeypatch):
    from genesis.payments import vault_service as vsvc

    monkeypatch.setattr("genesis.db.connection.get_db", AsyncMock(return_value=db))
    monkeypatch.setattr(vsvc.VaultService, "start", AsyncMock())
    registry, service = await ap.start_payment_services(MagicMock())
    assert isinstance(registry, PaymentApprovalRegistry) and registry._audit == service._audit


async def test_adapter_start_hands_the_registry_to_the_callback_handler(monkeypatch):
    """The real TelegramAdapterV2.start -> make_handlers_v2 -> HandlerContext path."""
    from genesis.channels.telegram import adapter_v2
    from genesis.channels.telegram.handlers_v2 import make_handlers_v2

    captured = {}

    class Stop(Exception):
        pass

    def capture(*args, **kw):
        captured["handlers"] = make_handlers_v2(*args, **kw)
        raise Stop

    monkeypatch.setattr(adapter_v2, "make_handlers_v2", capture)
    adapter = adapter_v2.TelegramAdapterV2(token="t", conversation_loop=MagicMock())
    registry = MagicMock(spec=PaymentApprovalRegistry)
    registry.handle_tap = AsyncMock(return_value=False)
    adapter.payment_approvals = registry
    with pytest.raises(Stop):
        await adapter.start()
    bot = FakeBot()
    query = CallbackQuery(
        id="q", from_user=User(id=OWNER, first_name="u", is_bot=False), chat_instance="c",
        data="payapprove:n",
    )
    query.set_bot(bot)
    await captured["handlers"]["callback_query"](Update(update_id=1, callback_query=query), None)
    registry.handle_tap.assert_awaited_once_with("n", OWNER, None)


async def test_start_telegram_attaches_the_bot_and_owner_after_start(monkeypatch):
    from genesis.channels.bridge_config import build_bridge_config
    from genesis.hosting.standalone import StandaloneAdapter

    monkeypatch.setenv("TTS_ENABLED", "false")
    config = build_bridge_config({"TELEGRAM_BOT_TOKEN": "t", "TELEGRAM_ALLOWED_USERS": "300,100"})
    host = StandaloneAdapter()
    host._runtime = MagicMock()
    host._payment_approvals = registry = PaymentApprovalRegistry()
    bot = FakeBot()
    seen = {}

    class Adapter:
        payment_approvals = None
        _app = None
        _stopping = False

        async def start(self):
            seen["registry"] = self.payment_approvals
            seen["bot_before_start"] = registry._bot
            self._app = MagicMock(bot=bot)

    with (
        patch("genesis.util.process_lock.ProcessLock.is_locked", return_value=False),
        patch("genesis.channels.bridge._load_bridge_config", return_value=config),
        patch("genesis.channels.bridge.create_telegram_adapter", return_value=Adapter()),
    ):
        await host._start_telegram()
    assert seen == {"registry": registry, "bot_before_start": None}
    assert registry._bot is bot and registry._owner == 300


# -- registration: never resolvable generically ---------------------------------


async def test_registered_in_the_denylist_and_refuse_generic():
    from genesis.autonomy.approval import ApprovalManager
    from genesis.autonomy.approval_gate import AutonomousCliApprovalGate
    from genesis.db.schema import create_all_tables

    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    try:
        await create_all_tables(conn)
        mgr = ApprovalManager(db=conn)
        pay = await mgr.request_approval(
            action_type=PAYMENT_APPROVAL_ACTION_TYPE,
            action_class="irreversible",
            description="card release",
            context=json.dumps({}),
        )
        control = await mgr.request_approval(
            action_type="autonomous_cli_fallback",
            action_class="costly_reversible",
            description="ordinary",
            context=json.dumps({}),
        )
        gate = AutonomousCliApprovalGate.__new__(AutonomousCliApprovalGate)
        gate._approval_manager = mgr
        assert await gate.approve_all_pending(resolved_by="telegram:batch:1") == 1
        assert (await mgr.get_by_id(control))["status"] == "approved"
        assert (await mgr.get_by_id(pay))["status"] == "pending"
        assert not await gate.resolve_request(pay, decision="approved", resolved_by="dash")
        assert (await mgr.get_by_id(pay))["status"] == "pending"
        assert PAYMENT_APPROVAL_ACTION_TYPE not in AutonomousCliApprovalGate._VOICE_GATED_TYPES
    finally:
        await conn.close()


@pytest.fixture
def client():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app.test_client()


_ROWS = [
    {"id": "p", "action_type": PAYMENT_APPROVAL_ACTION_TYPE, "context": "{}"},
    {"id": "c", "action_type": "autonomous_cli_fallback", "context": "{}"},
]


@pytest.mark.parametrize("route", ["/api/genesis/approvals", "/api/genesis/comms"])
def test_both_dashboard_filters_withhold_it(client, route):
    rt = MagicMock()
    rt.is_bootstrapped = True
    with (
        patch("genesis.runtime.GenesisRuntime") as runtime,
        patch(
            "genesis.db.crud.approval_requests.list_pending",
            new_callable=AsyncMock,
            return_value=[dict(r) for r in _ROWS],
        ),
    ):
        runtime.instance.return_value = rt
        body = client.get(route).get_json()
    rows = body if isinstance(body, list) else body["pending_approvals"]
    assert [r["id"] for r in rows] == ["c"]
