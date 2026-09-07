"""Tests for the DesktopTakeoverGate — the check between Genesis and the
operator's own keyboard, mouse and screen.

Real DB (full schema), real ApprovalManager, real capability CRUD; only the
event bus and the arming lever are stubbed. Every refusal test has a positive
control beside it: an inert check and a working check look identical from the
outside, and this is the one capability where that confusion is expensive.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import aiosqlite
import pytest

from genesis.autonomy import desktop_gate as dg
from genesis.autonomy.approval import ApprovalManager
from genesis.autonomy.desktop_gate import (
    DESKTOP_GATE_ACTION_TYPE,
    DesktopTakeoverGate,
    build_session_grant_context,
)
from genesis.autonomy.types import CellEvent, CellState
from genesis.db.crud import approval_requests as ar
from genesis.db.crud import capability_grants as cg
from genesis.db.schema import create_all_tables

_SESSION = "sess-abc"
_WINDOW = "Notepad"
_TS = "2026-06-21T00:00:00+00:00"


@pytest.fixture
async def db(tmp_path):
    conn = await aiosqlite.connect(str(tmp_path / "t.db"))
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    await conn.commit()
    yield conn
    await conn.close()


@pytest.fixture
def live(monkeypatch):
    """Arm the lever. Isolated from the real config so a test can never be
    reading the operator's own posture."""
    monkeypatch.setattr(dg, "effective_mode", lambda: "live")
    monkeypatch.setattr(dg, "grant_ttl_minutes", lambda: 30)
    monkeypatch.setattr(dg, "action_ttl_seconds", lambda: 30)


@pytest.fixture
def shadow(monkeypatch):
    """The SHIPPED posture, isolated from the real config."""
    monkeypatch.setattr(dg, "effective_mode", lambda: "shadow")
    monkeypatch.setattr(dg, "grant_ttl_minutes", lambda: 30)
    monkeypatch.setattr(dg, "action_ttl_seconds", lambda: 30)


async def _check(db, **kw):
    """``check()`` with the fixture's session and window filled in.

    The grant is per WINDOW. A test that does not care which window it is
    acting in must still act inside the GRANTED one — otherwise it silently
    measures the window bar instead of the thing its name claims.
    """
    kw.setdefault("session_id", _SESSION)
    kw.setdefault("window_title", _WINDOW)
    return await _gate(db).check(**kw)


def _gate(db):
    return DesktopTakeoverGate(db=db, approval_manager=ApprovalManager(db=db), event_bus=None)


async def _grant(
    db,
    *,
    session_id: str = _SESSION,
    resolved_by: str = "telegram:button:1",
    window_title: str = _WINDOW,
) -> str:
    """Create and resolve a session grant the way the PR-3 consent path will."""
    import json

    mgr = ApprovalManager(db=db)
    rid = await mgr.request_approval(
        action_type=DESKTOP_GATE_ACTION_TYPE,
        action_class="irreversible",
        description=f"Desktop control of '{window_title}'",
        context=json.dumps(
            build_session_grant_context(
                session_id=session_id, window_title=window_title, mission="tidy up"
            )
        ),
        timeout_seconds=None,
    )
    await mgr.resolve(rid, status="approved", resolved_by=resolved_by)
    return rid


# ═════════════════════════ the positive control ═══════════════════════════


@pytest.mark.asyncio
async def test_standard_action_under_a_live_grant_is_allowed(db, live):
    """Without this, every refusal below could be a check with no live path
    to refuse — the failure mode this whole file is organised around."""
    await _grant(db)
    decision = await _check(db,
        session_id=_SESSION,
        window_title="Notepad",
        element_name="Text Area",
        control_type="Edit",
        text="hello",
    )
    assert decision.allow is True
    assert decision.reason == "session_grant"
    assert decision.cell == ("desktop", "control", "standard")
    assert decision.expires_at is not None
    # The stamp must be in the future and bounded — the device refuses a
    # request past it, and an unbounded one would defeat that check.
    expires = datetime.fromisoformat(decision.expires_at)
    assert datetime.now(UTC) < expires <= datetime.now(UTC) + timedelta(seconds=31)


# ═════════════════════════ the arming lever ═══════════════════════════════


@pytest.mark.asyncio
async def test_unarmed_refuses_and_queues_nothing(db, monkeypatch):
    monkeypatch.setattr(dg, "effective_mode", lambda: "off")
    await _grant(db)
    decision = await _check(db, session_id=_SESSION, element_name="Text Area")
    assert decision.allow is False
    assert decision.reason == "not_armed"
    # An unarmed capability must not put work in front of the owner.
    assert await ar.list_pending(db) == []


@pytest.mark.asyncio
async def test_shadow_refuses_but_reports_what_live_would_do(db, shadow):
    await _grant(db)
    decision = await _check(db,
        session_id=_SESSION,
        window_title="Notepad",
        element_name="Text Area",
    )
    assert decision.allow is False
    assert decision.reason == "shadow"
    assert decision.would_allow is True  # distinguishable from a real refusal
    # Shadow observes; it does not create approvals for a capability that
    # cannot act.
    assert await ar.list_pending(db) == []


@pytest.mark.asyncio
async def test_shadow_still_records_the_cell(db, shadow):
    """The observation IS the cell — without it shadow mode watches nothing."""
    await _grant(db)
    await _check(db, session_id=_SESSION, element_name="Text Area")
    assert await cg.get_cell(db, "desktop", "control", "standard") is not None


@pytest.mark.asyncio
async def test_shadow_observes_the_state_a_shadow_INSTALL_is_actually_in(db, shadow):
    """No grant exists on a shadow install — nobody asks for keyboard consent
    for a capability that cannot act. An observer that only reports on sessions
    already holding a grant therefore observes nothing at all, which is the
    opposite of what config/desktop_takeover.yaml promises."""
    decision = await _check(db,
        session_id=_SESSION, window_title="Notepad", element_name="Text Area"
    )
    assert decision.reason == "shadow"
    assert decision.would_allow is False  # honest: live would have refused
    assert await cg.get_cell(db, "desktop", "control", "standard") is not None
    assert await ar.list_pending(db) == []


# ═════════════════════════ session consent ════════════════════════════════


@pytest.mark.asyncio
async def test_no_grant_refuses_and_writes_nothing(db, live):
    """An unauthorized caller must not be able to make the gate record
    anything on its behalf — no cell, no approval row."""
    decision = await _check(db, session_id=_SESSION, element_name="Text Area")
    assert decision.allow is False
    assert decision.reason == "no_session_grant"
    assert await cg.get_cell(db, "desktop", "control", "standard") is None
    assert await ar.list_pending(db) == []


@pytest.mark.asyncio
async def test_a_grant_for_another_session_does_not_authorise(db, live):
    await _grant(db, session_id="some-other-session")
    decision = await _check(db, session_id=_SESSION, element_name="Text Area")
    assert decision.allow is False
    assert decision.reason == "no_session_grant"


@pytest.mark.asyncio
async def test_genesis_self_approval_does_not_authorise(db, live):
    """`genesis:*` classifies as SYSTEM. Genesis approving itself into the
    operator's keyboard is the exact hole this bar exists to close."""
    await _grant(db, resolved_by="genesis:desktop-takeover")
    decision = await _check(db, session_id=_SESSION, element_name="Text Area")
    assert decision.allow is False
    assert decision.reason == "grant_not_human"


@pytest.mark.asyncio
async def test_unknown_resolver_does_not_authorise(db, live):
    """An unrecognised resolved_by is `unknown`, not `human` — the safe read
    of a writer nobody registered is 'not proven to be a person'."""
    await _grant(db, resolved_by="mystery_channel")
    decision = await _check(db, session_id=_SESSION, element_name="Text Area")
    assert decision.allow is False
    assert decision.reason == "grant_not_human"


@pytest.mark.asyncio
async def test_consumed_grant_does_not_authorise(db, live):
    """Consumption is how a session's grant is retired at teardown."""
    rid = await _grant(db)
    assert await ar.mark_consumed(db, rid, consumed_at=_TS) is True
    decision = await _check(db, session_id=_SESSION, element_name="Text Area")
    assert decision.allow is False
    assert decision.reason == "no_session_grant"


@pytest.mark.asyncio
async def test_expired_grant_does_not_authorise(db, live):
    """A grant nobody remembers giving must lapse on its own, without needing
    a teardown that may never run."""
    rid = await _grant(db)
    stale = (datetime.now(UTC) - timedelta(minutes=31)).isoformat()
    await db.execute("UPDATE approval_requests SET resolved_at = ? WHERE id = ?", (stale, rid))
    await db.commit()
    decision = await _check(db, session_id=_SESSION, element_name="Text Area")
    assert decision.allow is False
    assert decision.reason == "grant_expired"


@pytest.mark.asyncio
async def test_unageable_grant_does_not_authorise(db, live):
    """An approved row whose resolved_at cannot be parsed cannot be aged, and
    an un-ageable grant is an unbounded one."""
    rid = await _grant(db)
    await db.execute(
        "UPDATE approval_requests SET resolved_at = 'not a timestamp' WHERE id = ?",
        (rid,),
    )
    await db.commit()
    decision = await _check(db, session_id=_SESSION, element_name="Text Area")
    assert decision.allow is False
    assert decision.reason == "grant_expired"


@pytest.mark.asyncio
async def test_a_system_row_does_not_hide_a_valid_human_grant(db, live):
    """The lookup returns every match rather than the newest, so a later
    system-resolved row cannot mask the owner's real approval underneath."""
    await _grant(db, resolved_by="telegram:button:1")
    await _grant(db, resolved_by="genesis:desktop-takeover")
    decision = await _check(db, session_id=_SESSION, element_name="Text Area")
    assert decision.allow is True


@pytest.mark.asyncio
async def test_approving_one_held_action_does_not_grant_the_session(db, live):
    """MUST-FIX regression. Holds and grants are rows of the SAME action_type,
    so without the `kind` predicate an approved hold satisfies the grant
    lookup: the owner consents to one click and hands over the whole session,
    with a fresh TTL. MEASURED as allow=True before the fix."""
    rid = await _grant(db, window_title="Mail")
    held = await _check(db,
        session_id=_SESSION,
        window_title="Mail",
        element_name="Send",
        control_type="Button",
    )
    assert held.reason == "held"

    # Retire the real grant so the held row is the only candidate left.
    await ar.mark_consumed(db, rid, consumed_at=datetime.now(UTC).isoformat())
    stranded = await _check(db, session_id=_SESSION, window_title="Mail",
                            element_name="Text Area")
    assert stranded.reason == "no_session_grant"

    # The owner approves that ONE held action, through an allowlisted channel.
    await ApprovalManager(db=db).resolve(
        held.request_id, status="approved", resolved_by="telegram:button:2"
    )

    after = await _check(db,
        session_id=_SESSION,
        window_title="Mail",
        element_name="Text Area",
        control_type="Edit",
    )
    assert after.allow is False
    assert after.reason == "no_session_grant"


@pytest.mark.asyncio
async def test_a_dashboard_resolution_does_not_mint_a_grant(db, live):
    """`dashboard` classifies as HUMAN for metrics, and must not be enough
    here: the resolve route stamps it unconditionally and is reachable by any
    local process holding the internal bearer token, so it cannot tell the
    owner from Genesis."""
    await _grant(db, resolved_by="dashboard")
    decision = await _check(db, session_id=_SESSION, element_name="Text Area")
    assert decision.allow is False
    assert decision.reason == "grant_not_human"


@pytest.mark.asyncio
async def test_the_default_user_resolver_does_not_mint_a_grant(db, live):
    """`ApprovalManager.resolve`'s default is `resolved_by="user"`, which
    classifies as human. One forgotten kwarg must not be a desktop grant."""
    await _grant(db, resolved_by="user")
    decision = await _check(db, session_id=_SESSION, element_name="Text Area")
    assert decision.allow is False
    assert decision.reason == "grant_not_human"


def test_grant_resolvers_are_a_strict_narrowing_of_the_human_class():
    """The canonical mapping stays authoritative: this allowlist may only ever
    be NARROWER than HUMAN_RESOLVER_PREFIXES, never admit something the
    canonical mapping would not call human."""
    from genesis.autonomy.desktop_gate import DESKTOP_GRANT_RESOLVER_PREFIXES

    assert set(DESKTOP_GRANT_RESOLVER_PREFIXES) < set(ar.HUMAN_RESOLVER_PREFIXES)


@pytest.mark.asyncio
async def test_a_future_dated_grant_does_not_authorise(db, live):
    """A negative age passes an upper-bound-only check forever; a backwards
    clock step or a hand-edited row would mint a permanent grant."""
    rid = await _grant(db)
    ahead = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    await db.execute(
        "UPDATE approval_requests SET resolved_at = ? WHERE id = ?", (ahead, rid)
    )
    await db.commit()
    decision = await _check(db, session_id=_SESSION, element_name="Text Area")
    assert decision.allow is False
    assert decision.reason == "grant_expired"


@pytest.mark.asyncio
async def test_a_grant_for_one_window_does_not_authorise_another(db, live):
    """The grant NAMES a window and the consent card reads it back to the
    operator, so an uncompared field would be a promise the card makes and the
    code does not keep. Found by review: `window_title` was carried and
    displayed but never compared, so a grant for a text editor authorised
    actions in an unrelated chat or banking window."""
    await _grant(db, window_title="Notepad")

    same = await _check(db, window_title="Notepad", element_name="Text Area",
                        control_type="Edit")
    assert same.allow is True, "positive control: the granted window still works"

    for other in ("Slack - #general", "Online Banking", "A Different App"):
        d = await _check(db, window_title=other, element_name="Text Area",
                         control_type="Edit")
        assert d.allow is False, other
        assert d.reason == "grant_window_mismatch", other


@pytest.mark.asyncio
async def test_window_matching_ignores_case_and_surrounding_whitespace(db, live):
    """Normalisation, and nothing more. Two titles that differ only in case or
    padding are the same window; anything else is not."""
    await _grant(db, window_title="Notepad")
    d = await _check(db, window_title="  notepad  ", element_name="Text Area",
                     control_type="Edit")
    assert d.allow is True


@pytest.mark.asyncio
async def test_an_empty_window_is_a_refusal_not_a_wildcard(db, live):
    """The rule the device already applies to a target it cannot resolve: an
    absent target is no grant, never every grant."""
    await _grant(db, window_title="Notepad")
    d = await _check(db, window_title="", element_name="Text Area")
    assert d.allow is False
    assert d.reason == "grant_window_mismatch"


@pytest.mark.asyncio
async def test_a_grant_naming_no_window_authorises_nothing(db, live):
    """A grant with no window is not a narrower grant; it is an unbounded one.
    Reachable via a hand-written row or an older wire format, so it is refused
    rather than trusted."""
    import json

    rid = await _grant(db, window_title="Notepad")
    await db.execute(
        "UPDATE approval_requests SET context = ? WHERE id = ?",
        (json.dumps({"kind": dg.SESSION_GRANT_KIND, "session_id": _SESSION}), rid),
    )
    await db.commit()
    d = await _check(db, window_title="Notepad", element_name="Text Area")
    assert d.allow is False
    assert d.reason == "grant_window_mismatch"


@pytest.mark.asyncio
async def test_a_pending_request_is_not_a_grant(db, live):
    """Asking is not being told yes."""
    import json

    mgr = ApprovalManager(db=db)
    await mgr.request_approval(
        action_type=DESKTOP_GATE_ACTION_TYPE,
        action_class="irreversible",
        description="Desktop control",
        context=json.dumps(
            build_session_grant_context(
                session_id=_SESSION, window_title="Notepad", mission="tidy up"
            )
        ),
        timeout_seconds=None,
    )
    decision = await _check(db, session_id=_SESSION, element_name="Text Area")
    assert decision.allow is False
    assert decision.reason == "no_session_grant"


# ═════════════════════════ secret fields ══════════════════════════════════


@pytest.mark.asyncio
async def test_password_field_refuses_rather_than_holds(db, live):
    """There is no approval that makes this acceptable, so the gate must not
    offer the owner a button that says otherwise."""
    await _grant(db)
    decision = await _check(db,
        session_id=_SESSION,
        window_title="Login",
        element_name="Pwd",
        control_type="Edit",
        is_password=True,
        text="hunter2",
    )
    assert decision.allow is False
    assert decision.reason == "password_field"
    assert decision.request_id is None  # not a hold
    assert await ar.list_pending(db) == []  # nothing to approve
    # And no cell is created for a secret target.
    assert await cg.get_cell(db, "desktop", "control", "standard") is None


@pytest.mark.asyncio
async def test_password_named_target_refuses_without_the_flag(db, live):
    """Custom controls routinely do not expose IsPassword. The name of the
    resolved target is a second, independent bar."""
    await _grant(db)
    decision = await _check(db,
        session_id=_SESSION,
        window_title="Bank",
        element_name="Passphrase",
        control_type="Edit",
        is_password=False,
    )
    assert decision.allow is False
    assert decision.reason == "password_field"


@pytest.mark.asyncio
async def test_a_password_word_in_the_window_title_is_not_a_password_target(db, live):
    """Fail-closed on the TARGET, not the container: a window whose NAME says
    'password' must not make every control inside it unreachable.

    The window title carries the trigger word and the target does not, which is
    the only shape that can tell the two matchers apart. An earlier version used
    'Sign in to your account', where `sign` made the outcome a hold regardless —
    so it passed whether or not the matcher was scoped to the target, and could
    not fail for the invariant it named."""
    await _grant(db, window_title="Password Manager")
    decision = await _check(db,
        session_id=_SESSION,
        window_title="Password Manager",
        element_name="Search",
        control_type="Edit",
    )
    assert decision.reason != "password_field"
    assert decision.allow is True  # and it is an ordinary action, not a hold


@pytest.mark.asyncio
async def test_typing_an_identity_word_is_not_an_identity_action(db, live):
    """IDENTITY reads the CONTROL, not the typed text. Clicking 'Send' is the
    identity act; typing the word is not — and a gate that holds the most
    ordinary desktop action there is teaches people to wave it through."""
    await _grant(db)
    decision = await _check(db,
        session_id=_SESSION,
        window_title="Notepad",
        element_name="Text Area",
        control_type="Edit",
        text="please send me the file and delete the draft",
    )
    assert decision.allow is True
    assert decision.cell == ("desktop", "control", "standard")


@pytest.mark.asyncio
async def test_typed_card_details_still_classify_financial(db, live):
    """The exception that proves the rule above: money is dangerous as CONTENT,
    so FINANCIAL — and only FINANCIAL — still reads the typed text."""
    await _grant(db)
    decision = await _check(db,
        session_id=_SESSION,
        window_title="Notepad",
        element_name="Text Area",
        control_type="Edit",
        text="my routing number is 123456789",
    )
    assert decision.allow is False
    assert decision.cell == ("desktop", "control", "financial")


# ═════════════════════════ the risk gradient ══════════════════════════════


@pytest.mark.asyncio
async def test_identity_action_holds_under_a_live_grant(db, live):
    """Session consent covers ordinary input. Acting in the operator's name is
    its own decision, every time."""
    await _grant(db, window_title="Mail")
    decision = await _check(db,
        session_id=_SESSION,
        window_title="Mail",
        element_name="Send",
        control_type="Button",
    )
    assert decision.allow is False
    assert decision.reason == "held"
    assert decision.cell == ("desktop", "control", "identity")
    assert decision.request_id is not None


@pytest.mark.asyncio
async def test_financial_action_holds_under_a_live_grant(db, live):
    await _grant(db, window_title="Bank")
    decision = await _check(db,
        session_id=_SESSION,
        window_title="Bank",
        element_name="Confirm payment",
        control_type="Button",
    )
    assert decision.allow is False
    assert decision.reason == "held"
    assert decision.cell == ("desktop", "control", "financial")


@pytest.mark.asyncio
async def test_a_hold_queues_nothing_resumable(db, live):
    """No pending-action table and no drain: approving the row does not replay
    the action, because by then the screen it targeted has moved."""
    await _grant(db, window_title="Mail")
    decision = await _check(db,
        session_id=_SESSION,
        window_title="Mail",
        element_name="Send",
        control_type="Button",
    )
    row = await ar.get_by_id(db, decision.request_id)
    assert row["action_type"] == DESKTOP_GATE_ACTION_TYPE
    # A held desktop action waits forever for the owner — never auto-approved,
    # never auto-dropped. expire_timed_out skips NULL timeout_at rows.
    assert row["timeout_at"] is None
    n = await ar.expire_timed_out(db, now=(datetime.now(UTC) + timedelta(days=365)).isoformat())
    assert n == 0
    assert (await ar.get_by_id(db, decision.request_id))["status"] == "pending"


@pytest.mark.asyncio
async def test_screen_text_cannot_forge_lines_in_the_approval_the_owner_reads(db, live):
    """Window titles and element names come off a screen this threat model
    treats as hostile, and they flow into the sentence a human reads before
    deciding. Newlines could forge extra lines in a rendered card, bidi
    overrides could reorder what is displayed away from what is approved, and
    zero-width characters could conceal either."""
    hostile = "Mail\n\nAPPROVED: routine\u202egnihtemos esle"
    await _grant(db, window_title=hostile)
    decision = await _check(db,
        session_id=_SESSION,
        window_title=hostile,
        element_name="Send\u200b\u200b",
        control_type="Button",
    )
    row = await ar.get_by_id(db, decision.request_id)
    desc = row["description"]
    assert "\n" not in desc
    assert "\u202e" not in desc and "\u200b" not in desc
    assert "Send" in desc  # the legible content survives


@pytest.mark.asyncio
async def test_a_hostile_window_title_cannot_flood_the_approval_row(db, live):
    """Bounded as a PREVIEW, not amputated: the full value stays verbatim in
    the row's context, so nothing is lost."""
    import json

    huge = "A" * 5000
    await _grant(db, window_title=huge)
    decision = await _check(db,
        session_id=_SESSION, window_title=huge, element_name="Send",
        control_type="Button",
    )
    row = await ar.get_by_id(db, decision.request_id)
    assert len(row["description"]) < 500
    assert "more chars>" in row["description"]  # the cut is DECLARED
    assert json.loads(row["context"])["window_title"] == huge  # nothing lost


@pytest.mark.asyncio
async def test_a_malformed_context_row_does_not_break_the_grant_lookup(db, live):
    """One hand-edited or corrupted `context` anywhere in the table made every
    desktop grant lookup raise `malformed JSON` — MEASURED before the CASE/
    json_valid guard. It failed closed, but a gate that crashes is a gate
    nobody can use."""
    await _grant(db)
    for rid, atype in (("bad1", "autonomous_cli_fallback"), ("bad2", DESKTOP_GATE_ACTION_TYPE)):
        await db.execute(
            "INSERT INTO approval_requests (id, action_type, action_class, "
            "description, context, status) VALUES (?, ?, 'reversible', 'x', "
            "'{not json', 'approved')",
            (rid, atype),
        )
    await db.commit()
    decision = await _check(db, session_id=_SESSION, element_name="Text Area")
    assert decision.allow is True


# ═════════════════════ classification, adversarially ══════════════════════


@pytest.mark.asyncio
async def test_a_typed_card_number_classifies_financial_by_SHAPE(db, live):
    """"FINANCIAL also reads the typed text" was an empty promise while the
    patterns were all label-shaped: a real card number typed into a field
    labelled "Confirmation" matched nothing and passed as STANDARD."""
    await _grant(db)
    decision = await _check(db,
        session_id=_SESSION, window_title="Notepad", element_name="Confirmation",
        control_type="Edit", text="4111 1111 1111 1111",
    )
    assert decision.allow is False
    assert decision.cell == ("desktop", "control", "financial")


@pytest.mark.asyncio
async def test_an_ordinary_number_is_not_financial(db, live):
    """The other direction: fail-closed must not mean every digit holds."""
    await _grant(db)
    decision = await _check(db,
        session_id=_SESSION, window_title="Notepad", element_name="Text Area",
        control_type="Edit", text="the meeting is at 3pm in room 214",
    )
    assert decision.allow is True


@pytest.mark.parametrize(
    "name", ["Create account", "New account", "Sign up", "Register"]
)
def test_account_creation_crosses_the_identity_bar(name):
    """Creating an account acts in the operator's name as much as sending does.
    It was previously held only by accident — when the button happened to read
    "Submit" or "Sign up" — which is exactly the region the classifier claims
    to cover."""
    from genesis.autonomy.classification import classify_desktop_action

    c = classify_desktop_action(element_name=name, control_type="Button")
    assert str(c.risk_class) == "identity", name


@pytest.mark.parametrize("name", ["Create folder", "New document", "New tab"])
def test_ordinary_create_actions_are_not_identity(name):
    """The positive control: "create" alone must not cross the bar, or every
    file operation holds and the gate teaches people to wave it through."""
    from genesis.autonomy.classification import classify_desktop_action

    c = classify_desktop_action(element_name=name, control_type="Button")
    assert str(c.risk_class) == "standard", name


@pytest.mark.parametrize(
    "name",
    ["OTP", "One-time code", "2FA code", "Security question", "Recovery key",
     "Passkey", "PIN", "Social Security number"],
)
def test_the_secret_field_family_is_covered_not_just_the_word_password(name):
    """For anything off this list the accessibility flag is the only backstop —
    and the reason the list exists is that the flag is unreliable."""
    from genesis.autonomy.classification import classify_desktop_action

    c = classify_desktop_action(element_name=name, control_type="Edit")
    assert c.is_password is True, name


def test_ordinary_controls_are_not_treated_as_secret_fields():
    """The positive control: a list that matches everything protects nothing."""
    from genesis.autonomy.classification import classify_desktop_action

    for name in ("Search", "Username", "Text Area", "Subject", "To"):
        assert classify_desktop_action(element_name=name).is_password is False, name


# ═══════════════════ the cell can deny, never grant ═══════════════════════


@pytest.mark.asyncio
async def test_denied_permanent_cell_outranks_a_live_grant(db, live):
    """The owner's standing 'not this, ever'."""
    await _grant(db)
    now = datetime.now(UTC).isoformat()
    await cg.apply_event(
        db,
        domain="desktop",
        verb="control",
        risk_class="standard",
        event=CellEvent.CLASSIFY,
        updated_at=now,
        origin_class="owner",
    )
    await cg.apply_event(
        db,
        domain="desktop",
        verb="control",
        risk_class="standard",
        event=CellEvent.DENY_PERMANENT,
        updated_at=now,
        origin_class="owner",
    )
    decision = await _check(db, session_id=_SESSION, element_name="Text Area")
    assert decision.allow is False
    assert decision.reason == "denied_permanent"


@pytest.mark.asyncio
async def test_a_desktop_cell_can_never_be_promoted(db):
    """The acceptance bar inherited from PR #1838: banked evidence must never
    turn session consent into standing autonomy. The email control proves the
    scan is live rather than returning nothing at all."""
    now = datetime.now(UTC).isoformat()
    for domain in ("desktop", "email"):
        verb = "control" if domain == "desktop" else "send"
        await cg.apply_event(
            db,
            domain=domain,
            verb=verb,
            risk_class="standard",
            event=CellEvent.CLASSIFY,
            updated_at=now,
            origin_class="owner",
        )
        for _ in range(10):
            await cg.record_success(
                db,
                domain=domain,
                verb=verb,
                risk_class="standard",
                updated_at=now,
                origin_class="owner",
            )

    candidates = {(c["domain"], c["verb"]) for c in await cg.detect_promotable_cells(db)}
    assert ("email", "send") in candidates  # positive control: scan is live
    assert ("desktop", "control") not in candidates

    assert cg.is_promotable_cell("desktop", "standard") is False
    assert "desktop" not in cg.PROMOTABLE_DOMAINS

    # And the backstop below the scan refuses the promotion outright.
    from genesis.autonomy.capabilities import InvalidTransition

    with pytest.raises(InvalidTransition):
        await cg.apply_event(
            db,
            domain="desktop",
            verb="control",
            risk_class="standard",
            event=CellEvent.APPROVE,
            updated_at=now,
            origin_class="owner",
        )
    cell = await cg.get_cell(db, "desktop", "control", "standard")
    assert cell["state"] == CellState.ASK.value


# ═══════════════════ the batch-approval bypasses ══════════════════════════


@pytest.mark.asyncio
async def test_approve_all_pending_never_grants_desktop_control(db):
    """One 'Approve all' tap must not hand over the keyboard. The sweep has no
    action-type allowlist, so this exclusion is the only thing stopping it."""
    from unittest.mock import MagicMock

    from genesis.autonomy.approval_gate import AutonomousCliApprovalGate

    mgr = ApprovalManager(db=db)
    desktop_rid = await mgr.request_approval(
        action_type=DESKTOP_GATE_ACTION_TYPE,
        action_class="irreversible",
        description="Desktop control of 'Notepad'",
    )
    other_rid = await mgr.request_approval(
        action_type="autonomous_cli_fallback",
        action_class="reversible",
        description="cli action",
    )
    gate = AutonomousCliApprovalGate(runtime=MagicMock(), approval_manager=mgr)

    count = await gate.approve_all_pending(resolved_by="dashboard:batch")

    assert count == 1  # positive control: the sweep DID run
    assert (await ar.get_by_id(db, other_rid))["status"] == "approved"
    assert (await ar.get_by_id(db, desktop_rid))["status"] == "pending"


@pytest.mark.asyncio
async def test_bare_telegram_approve_cannot_resolve_desktop(db):
    """The Telegram bare-'approve' handler resolves the most recent pending
    item. It filters to `autonomous_cli_fallback` by an inline literal, so
    desktop is excluded by construction — pinned BEHAVIOURALLY rather than by
    asserting the literal, because what matters is that a desktop row cannot be
    reached, not how the filter is spelled."""
    from unittest.mock import MagicMock

    from genesis.autonomy.approval_gate import AutonomousCliApprovalGate

    mgr = ApprovalManager(db=db)
    cli_rid = await mgr.request_approval(
        action_type="autonomous_cli_fallback",
        action_class="reversible",
        description="cli action",
    )
    desktop_rid = await mgr.request_approval(
        action_type=DESKTOP_GATE_ACTION_TYPE,
        action_class="irreversible",
        description="Desktop control",
    )  # created LAST, so a naive "most recent pending" would pick this one
    gate = AutonomousCliApprovalGate(runtime=MagicMock(), approval_manager=mgr)

    resolved = await gate.resolve_most_recent_pending(
        decision="approved",
        resolved_by="telegram:bare_text:1",
    )

    assert resolved == cli_rid  # positive control: it DID resolve something
    assert (await ar.get_by_id(db, desktop_rid))["status"] == "pending"


@pytest.mark.asyncio
async def test_the_generic_per_item_resolver_refuses_desktop(db):
    """`resolve_request` is the funnel for the dashboard's per-item Approve
    button, Telegram `cli_approve`, AND the `cli_approve_all` button's own
    trigger row — that last one resolves directly, sidestepping
    `approve_all_pending`'s exclusion set, so the exclusion must exist at both.
    Handing over the keyboard cannot come from a generic 'approve this id'."""
    from unittest.mock import MagicMock

    from genesis.autonomy.approval_gate import AutonomousCliApprovalGate

    mgr = ApprovalManager(db=db)
    desktop_rid = await mgr.request_approval(
        action_type=DESKTOP_GATE_ACTION_TYPE,
        action_class="irreversible",
        description="Desktop control",
    )
    cli_rid = await mgr.request_approval(
        action_type="autonomous_cli_fallback",
        action_class="reversible",
        description="cli action",
    )
    gate = AutonomousCliApprovalGate(runtime=MagicMock(), approval_manager=mgr)

    assert await gate.resolve_request(
        cli_rid, decision="approved", resolved_by="telegram:batch:1"
    ) is True  # positive control: the path works
    assert await gate.resolve_request(
        desktop_rid, decision="approved", resolved_by="telegram:batch:1"
    ) is False
    assert (await ar.get_by_id(db, desktop_rid))["status"] == "pending"


def test_desktop_rows_never_reach_the_generic_dashboard_queue():
    """The dashboard renders every pending row as a CLI-FALLBACK card: the
    template fills Fallback / Reason / API Route from context keys a desktop
    row does not have, so it would be presented as `claude -p` /
    "CLI fallback requires manual approval". An owner tapping Approve on that
    believes they cleared a stuck dispatch while handing over their keyboard."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from flask import Flask

    from genesis.dashboard.api import blueprint

    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True

    rows = [
        {"id": "a", "action_type": DESKTOP_GATE_ACTION_TYPE, "context": "{}",
         "description": "Desktop control of 'Notepad'", "created_at": _TS},
        {"id": "b", "action_type": "autonomous_cli_fallback", "context": "{}",
         "description": "cli action", "created_at": _TS},
    ]
    mock_rt = MagicMock()
    mock_rt.is_bootstrapped = True

    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch(
            "genesis.db.crud.approval_requests.list_pending",
            AsyncMock(return_value=rows),
        ),
    ):
        MockRT.instance.return_value = mock_rt
        resp = app.test_client().get("/api/genesis/approvals")

    assert resp.status_code == 200
    ids = {r["id"] for r in resp.get_json()}
    assert "b" in ids  # positive control: the queue DID render
    assert "a" not in ids


def test_voice_bare_approve_cannot_resolve_desktop():
    """_VOICE_GATED_TYPES is an allowlist, so desktop is excluded by
    construction — pinned so that adding it is a deliberate act. The generic
    bare-'approve' resolver acts on the most recent pending item, so a spoken
    'yes' aimed at something else must never reach a desktop grant."""
    from genesis.autonomy.approval_gate import AutonomousCliApprovalGate

    assert sorted(AutonomousCliApprovalGate._VOICE_GATED_TYPES) == [
        "autonomous_cli_fallback",
        "build_greenlight",
        "sentinel_action",
        "sentinel_dispatch",
    ]
    assert DESKTOP_GATE_ACTION_TYPE not in AutonomousCliApprovalGate._VOICE_GATED_TYPES


def test_desktop_action_type_has_no_configured_timeout():
    """`timeout_seconds=None` waits forever only while this action type is
    absent from BOTH timeout tables. Adding it there would silently start
    auto-expiring desktop approvals."""
    from genesis.autonomy.classification import (
        _DEFAULT_APPROVAL_TIMEOUTS,
        ActionClassifier,
    )

    assert DESKTOP_GATE_ACTION_TYPE not in _DEFAULT_APPROVAL_TIMEOUTS
    assert ActionClassifier().get_timeout(DESKTOP_GATE_ACTION_TYPE) is None
