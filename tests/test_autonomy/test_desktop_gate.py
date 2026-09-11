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
from genesis.autonomy import desktop_takeover_config as dtc
from genesis.autonomy.approval import ApprovalManager
from genesis.autonomy.classification import (
    DesktopAction,
    DesktopActionClassification,
    DesktopOperation,
)
from genesis.autonomy.desktop_gate import (
    DESKTOP_GATE_ACTION_TYPE,
    DesktopTakeoverGate,
    build_session_grant_context,
)
from genesis.autonomy.types import ActionClass, CellEvent, CellState, RiskClass
from genesis.db.crud import approval_requests as ar
from genesis.db.crud import capability_grants as cg
from genesis.db.schema import create_all_tables

_SESSION = "sess-abc"
_WINDOW = "Notepad"
_TS = "2026-06-21T00:00:00+00:00"
#: Window IDENTITY is the (handle, pid) pair. The title is display only — two
#: browser tabs are both "New Tab", and a title changes when its document does.
_HANDLE = "0x000A1B2C"
_PID = 4242
_MISSION_ID = "mis-0001"
#: The LIFETIME half of window identity. A handle is recycled when its
#: window closes, and the pid does not help when the process outlives the
#: window — so the pair names a handle SLOT, not the window in it.
_NONCE = "win-nonce-0001"


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


def _action(**kw) -> DesktopAction:
    """A well-formed DesktopAction with the fixture's window filled in.

    ``control_type`` defaults to a neutral "Button" rather than "": a click
    with BOTH element_name and control_type blank is an unresolved target and
    is refused, so a test leaving them empty would measure that refusal instead
    of whatever it is named for.
    """
    kw.setdefault("operation", DesktopOperation.CLICK)
    kw.setdefault("window_handle", _HANDLE)
    kw.setdefault("process_id", _PID)
    kw.setdefault("window_nonce", _NONCE)
    kw.setdefault("window_title", _WINDOW)
    kw.setdefault("element_name", "")
    kw.setdefault("control_type", "Button")
    return DesktopAction(**kw)


def _classify(**kw):
    """``classify_desktop_action`` over a well-formed action.

    The classifier takes ONE object now, so the pure tests build one too rather
    than each re-listing the required fields — which is what let the old
    all-defaults signature hide a caller that never said what it was doing.
    """
    from genesis.autonomy.classification import classify_desktop_action

    return classify_desktop_action(_action(**kw))


async def _check(db, **kw):
    """``check()`` with the fixture's session, mission and window filled in.

    The grant is per (session, mission, window). A test that does not care
    which of those it is acting under must still act inside the GRANTED one —
    otherwise it silently measures a scope bar instead of the thing its name
    claims.
    """
    session_id = kw.pop("session_id", _SESSION)
    mission_id = kw.pop("mission_id", _MISSION_ID)
    return await _gate(db).check(
        _action(**kw), session_id=session_id, mission_id=mission_id
    )


def _gate(db):
    return DesktopTakeoverGate(db=db, approval_manager=ApprovalManager(db=db), event_bus=None)


async def _grant(
    db,
    *,
    session_id: str = _SESSION,
    resolved_by: str = "telegram:button:1",
    window_title: str = _WINDOW,
    window_handle: str = _HANDLE,
    process_id: int = _PID,
    window_nonce: str = _NONCE,
    mission_id: str = _MISSION_ID,
    mission: str = "tidy up",
    context: dict | None = None,
) -> str:
    """Create and resolve a session grant the way the PR-3 consent path will.

    *context* overrides the whole blob, for the tests that need a malformed or
    hand-written one.
    """
    import json

    mgr = ApprovalManager(db=db)
    blob = (
        context
        if context is not None
        else build_session_grant_context(
            session_id=session_id,
            mission_id=mission_id,
            mission=mission,
            window_handle=window_handle,
            process_id=process_id,
            window_nonce=window_nonce,
            window_title=window_title,
        )
    )
    rid = await mgr.request_approval(
        action_type=DESKTOP_GATE_ACTION_TYPE,
        action_class="irreversible",
        description=f"Desktop control of '{window_title}'",
        context=json.dumps(blob),
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
    code does not keep. Found by review: the window was carried and displayed
    but never compared, so a grant for a text editor authorised actions in an
    unrelated chat or banking window."""
    await _grant(db, window_handle="0xAAA1", process_id=100)

    same = await _check(db, window_handle="0xAAA1", process_id=100,
                        element_name="Text Area", control_type="Edit")
    assert same.allow is True, "positive control: the granted window still works"

    for other, pid in (("0xBBB2", 100), ("0xAAA1", 200), ("0xCCC3", 300)):
        d = await _check(db, window_handle=other, process_id=pid,
                         element_name="Text Area", control_type="Edit")
        assert d.allow is False, (other, pid)
        assert d.reason == "grant_window_mismatch", (other, pid)


@pytest.mark.asyncio
async def test_two_windows_with_the_SAME_TITLE_do_not_share_a_grant(db, live):
    """Window identity is (handle, pid), and the title is display only.

    This replaces a retired test that asserted title matching ignored case and
    padding. That test cannot be written against this design and, worse, it
    PASSES vacuously here — with the title no longer compared, any title is
    accepted, so it would go green with the mechanism it named deleted.

    The real hazard the title bar could never catch: two browser tabs are both
    called "New Tab". Under a title bar they share one grant."""
    await _grant(db, window_handle="0xTAB1", process_id=900, window_title="New Tab")

    same = await _check(db, window_handle="0xTAB1", process_id=900,
                        window_title="New Tab", element_name="Link", control_type="Hyperlink")
    assert same.allow is True, "positive control: the granted tab still works"

    other = await _check(db, window_handle="0xTAB2", process_id=900,
                         window_title="New Tab", element_name="Link",
                         control_type="Hyperlink")
    assert other.allow is False, "an identically-titled sibling tab is a DIFFERENT window"
    assert other.reason == "grant_window_mismatch"


@pytest.mark.asyncio
async def test_a_title_that_drifts_does_not_revoke_its_own_grant(db, live):
    """The other direction, and the reason the title cannot be the bar: a
    window's title changes when the document inside it does. Under a title bar
    the SAME window stops matching the grant it was given, and the operator is
    re-asked for a window they never left."""
    await _grant(db, window_handle="0xDOC1", process_id=901, window_title="Untitled - Notepad")
    d = await _check(db, window_handle="0xDOC1", process_id=901,
                     window_title="report-final.txt - Notepad",
                     element_name="Text Area", control_type="Edit")
    assert d.allow is True


@pytest.mark.asyncio
async def test_an_empty_window_handle_is_a_malformed_call(db, live):
    """The rule the device already applies to a target it cannot resolve: an
    absent target is no grant, never every grant. It is refused as a CALLER
    defect rather than compared, because a blank handle would otherwise be
    compared against a blank handle and match."""
    await _grant(db)
    d = await _check(db, window_handle="", element_name="Text Area")
    assert d.allow is False
    assert d.reason == "malformed_action:window_handle"


@pytest.mark.asyncio
async def test_a_grant_naming_no_window_authorises_nothing(db, live):
    """A grant with no window is not a narrower grant; it is an unbounded one.
    Reachable via a hand-written row or an older wire format, so it is refused
    rather than trusted."""
    import json

    rid = await _grant(db)
    await db.execute(
        "UPDATE approval_requests SET context = ? WHERE id = ?",
        (
            json.dumps({
                "kind": dg.SESSION_GRANT_KIND,
                "version": dg.SESSION_GRANT_VERSION,
                "session_id": _SESSION,
                "mission_id": _MISSION_ID,
            }),
            rid,
        ),
    )
    await db.commit()
    d = await _check(db, element_name="Text Area")
    assert d.allow is False
    assert d.reason == "grant_malformed"


@pytest.mark.asyncio
async def test_a_grant_with_a_BLANK_window_is_malformed_not_merely_mismatched(db, live):
    """Isolates the blank-required-field check in ``SessionGrant.parse``.

    The sibling test above omits ``process_id`` entirely, so ``int(None)``
    raises and parse refuses on the PID check — the blank-field check never
    decides anything there, and a mutation removing it survived. This fixture
    supplies a valid pid so the blank handle is the only thing wrong.

    Honest about what this is: the ACTION side of every blank is already
    refused by ``_validate_call`` (``no_session_id`` / ``no_mission_id`` /
    ``malformed_action:window_handle``), and a blank grant field can never
    match a non-blank action field. So this check is defence in depth for
    ``parse``'s own contract — constructing a SessionGrant IS the validation,
    and the PR-3 consent path will construct them. It is not the layer holding
    the security property; the caller-side checks are, and they have their own
    mutations."""
    import json

    rid = await _grant(db)
    ctx = build_session_grant_context(
        session_id=_SESSION, mission_id=_MISSION_ID, mission="m",
        window_handle="", process_id=_PID, window_nonce=_NONCE, window_title=_WINDOW,
    )
    await db.execute(
        "UPDATE approval_requests SET context = ? WHERE id = ?", (json.dumps(ctx), rid)
    )
    await db.commit()
    d = await _check(db, element_name="Text Area")
    assert d.allow is False
    assert d.reason == "grant_malformed", (
        "a blank required field makes it not-a-grant, not a grant for elsewhere"
    )


@pytest.mark.asyncio
async def test_a_grant_for_one_mission_does_not_authorise_another(db, live):
    """Consent is per MISSION. Without this bar, one approval covers whatever
    the loop decides to do next — the session grant becomes standing authority
    for anything, which is the shape the window bar exists to prevent one level
    down."""
    await _grant(db, mission_id="mis-tidy")

    same = await _check(db, mission_id="mis-tidy", element_name="Text Area",
                        control_type="Edit")
    assert same.allow is True, "positive control: the granted mission still works"

    other = await _check(db, mission_id="mis-something-else",
                         element_name="Text Area", control_type="Edit")
    assert other.allow is False
    assert other.reason == "grant_mission_mismatch"


@pytest.mark.asyncio
async def test_the_mission_is_bound_by_ID_not_by_its_PROSE(db, live):
    """The negative control for the bar above, and the reason it binds an id.

    Mission text is model-generated and is rewritten on every re-plan. Binding
    the prose would revoke a grant every time the loop rephrased its own plan —
    the window-title failure, one level up."""
    await _grant(db, mission_id="mis-tidy", mission="tidy up the desktop")
    d = await _check(db, mission_id="mis-tidy", element_name="Text Area",
                     control_type="Edit")
    assert d.allow is True, "same mission id, differently-worded plan, still granted"


@pytest.mark.asyncio
async def test_a_blank_session_id_does_not_match_a_blank_grant(db, live):
    """SQLite says `'' = ''`, so a grant carrying an empty session id would
    authorise a caller carrying an empty one. An ABSENT key extracts NULL and
    never matches, which is exactly why this looks safe and is not."""
    import json

    rid = await _grant(db)
    await db.execute(
        "UPDATE approval_requests SET context = ? WHERE id = ?",
        (
            json.dumps(
                build_session_grant_context(
                    session_id="", mission_id=_MISSION_ID, mission="m",
                    window_handle=_HANDLE, process_id=_PID, window_nonce=_NONCE, window_title=_WINDOW,
                )
            ),
            rid,
        ),
    )
    await db.commit()
    d = await _check(db, session_id="", element_name="Text Area")
    assert d.allow is False
    assert d.reason == "no_session_id"


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
                session_id=_SESSION, mission_id=_MISSION_ID, mission="tidy up",
                window_handle=_HANDLE, process_id=_PID, window_nonce=_NONCE, window_title=_WINDOW,
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

    # The invariant is NEVER AUTO-APPROVED, which is not the same as never
    # expiring — and this test used to assert the second, by pinning
    # `timeout_at is None`. That made the row immortal: every resolution path
    # refuses or excludes desktop rows, and `expire_timed_out` skips a null
    # timeout, so nothing in the system could move it out of `pending`. Since
    # `list_pending` is oldest-first and the morning report renders the oldest
    # five, a retrying loop would have occupied the operator's daily report
    # permanently.
    assert row["timeout_at"] is not None, "a hold must be able to lapse"

    n = await ar.expire_timed_out(db, now=(datetime.now(UTC) + timedelta(days=365)).isoformat())
    assert n == 1
    lapsed = await ar.get_by_id(db, decision.request_id)
    assert lapsed["status"] == "expired", "lapsed, NOT approved — the safety half"
    assert lapsed["status"] != "approved"

    # And the original point still holds: nothing was queued for replay.
    assert decision.request_id is not None
    assert await ar.find_approved_unconsumed(
        db, subsystem="desktop", policy_id="whatever"
    ) is None


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
    # The card is now newline-DELIMITED \u2014 one field per line \u2014 because a
    # newline is the one separator `_display` guarantees screen text cannot
    # contain, and an inline-quoted field could be forged with an apostrophe.
    # So the invariant is no longer "no newlines"; it is that screen text
    # cannot CHANGE the structure. That is strictly the stronger claim: the
    # old assertion would pass on a card whose fields had been reordered.
    lines = desc.split("\n")
    assert len(lines) == 4, lines
    assert lines[0].startswith("Desktop ") and lines[0].endswith(" action.")
    assert len([ln for ln in lines if ln.startswith("Window: ")]) == 1
    assert len([ln for ln in lines if ln.startswith("Control: ")]) == 1
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
    assert str(_classify(element_name=name, control_type="Button").risk_class) == "identity", name


@pytest.mark.parametrize("name", ["Create folder", "New document", "New tab"])
def test_ordinary_create_actions_are_not_identity(name):
    """The positive control: "create" alone must not cross the bar, or every
    file operation holds and the gate teaches people to wave it through."""
    assert str(_classify(element_name=name, control_type="Button").risk_class) == "standard", name


@pytest.mark.parametrize(
    "name",
    ["OTP", "One-time code", "2FA code", "Security question", "Recovery key",
     "Passkey", "PIN", "Social Security number"],
)
def test_the_secret_field_family_is_covered_not_just_the_word_password(name):
    """For anything off this list the accessibility flag is the only backstop —
    and the reason the list exists is that the flag is unreliable."""
    assert _classify(element_name=name, control_type="Edit").is_password is True, name


def test_ordinary_controls_are_not_treated_as_secret_fields():
    """The positive control: a list that matches everything protects nothing."""
    for name in ("Search", "Username", "Text Area", "Subject", "To"):
        assert _classify(element_name=name, control_type="Edit").is_password is False, name


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

    assert cg.is_promotable_cell("desktop", "control", "standard") is False
    assert "desktop" not in cg.PROMOTABLE_DOMAINS
    # The allowlist is keyed on (domain, verb), so assert the PAIR is absent —
    # a domain-only check would still pass if someone added ("desktop", <verb>).
    assert not any(d == "desktop" for d, _ in cg.PROMOTABLE_CELLS)

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
    # BOTH tables, as the docstring says. `get_timeout` is a `dict.get`, so it
    # returns None for "banana" too — asserting it alone cannot tell absent from
    # present-with-an-explicit-null, which is the distinction being claimed.
    assert DESKTOP_GATE_ACTION_TYPE not in ActionClassifier()._approval_timeouts
    assert ActionClassifier().get_timeout(DESKTOP_GATE_ACTION_TYPE) is None


# ═══════════ the input contract — what the old signature could not say ═══════


@pytest.mark.asyncio
async def test_a_key_chord_is_not_ordinary_typing(db, live):
    """The finding that settled the redesign. `check()` could not carry a key
    chord, so Ctrl+Enter in a mail composer — which SENDS — was indistinguishable
    from typing a letter. No pattern list fixes that: the information was not in
    the input type."""
    await _grant(db)
    d = await _check(db, operation=DesktopOperation.KEY, key_chord="ctrl+enter",
                     element_name="", control_type="")
    assert d.allow is False
    assert d.reason == "held"


@pytest.mark.asyncio
async def test_an_inert_chord_is_still_ordinary(db, live):
    """The positive control. An allowlist that holds every keystroke is a gate
    nobody leaves armed — navigation and text editing must stay free."""
    await _grant(db)
    for chord in ("ctrl+c", "shift+control+z", "tab", "up", "ctrl+s", "backspace"):
        d = await _check(db, operation=DesktopOperation.KEY, key_chord=chord,
                         element_name="", control_type="")
        assert d.allow is True, chord


@pytest.mark.asyncio
async def test_an_unknown_key_chord_holds_rather_than_passes(db, live):
    """ALLOWLIST polarity, which is the whole point: an unrecognised chord is
    held. A denylist's misses are vulnerabilities; this list's are one hold."""
    await _grant(db)
    d = await _check(db, operation=DesktopOperation.KEY, key_chord="ctrl+alt+q",
                     element_name="", control_type="")
    assert d.allow is False
    assert d.reason == "held"


@pytest.mark.asyncio
async def test_a_control_labelled_Pay_is_not_ordinary(db, live):
    """`Pay` and `Remove account` are on NEITHER desktop pattern list, while
    `classify_action` has always called both IRREVERSIBLE. Two classifiers
    disagreed about the same control and only one of them fed the risk."""
    await _grant(db)
    for label in ("Pay", "Remove account"):
        d = await _check(db, element_name=label, control_type="Button")
        assert d.allow is False, label
        assert d.reason == "held", label


@pytest.mark.asyncio
async def test_a_drag_holds_because_no_label_describes_it(db, live):
    """A drag acts on geometry. Dropping a folder onto another folder MOVES it,
    and the accessibility tree reports what it reports for dragging a
    scrollbar."""
    await _grant(db)
    d = await _check(db, operation=DesktopOperation.DRAG, element_name="Reports",
                     control_type="ListItem")
    assert d.allow is False
    assert d.reason == "held"


@pytest.mark.asyncio
async def test_a_blind_click_at_coordinates_is_refused(db, live):
    """Both element_name and control_type blank is a click at raw coordinates:
    the classifier cannot see it and the operator cannot be shown it. It used
    to be authorised like any other ordinary action."""
    await _grant(db)
    d = await _check(db, element_name="", control_type="")
    assert d.allow is False
    assert d.reason == "unresolved_target"


@pytest.mark.asyncio
async def test_a_key_action_with_no_chord_is_refused(db, live):
    """A KEY operation carrying no chord cannot be classified at all."""
    await _grant(db)
    d = await _check(db, operation=DesktopOperation.KEY, key_chord="",
                     element_name="", control_type="")
    assert d.allow is False
    assert d.reason == "malformed_action:key_chord"


@pytest.mark.asyncio
async def test_an_operation_outside_the_enum_is_refused_not_classified(db, live):
    """A CLOSED set. An operation nobody has reasoned about is the one case
    where guessing is guaranteed wrong — whoever added it knows something the
    risk table does not."""
    await _grant(db)
    d = await _check(db, operation="teleport", element_name="Save",
                     control_type="Button")
    assert d.allow is False
    assert d.reason == "unknown_operation"


@pytest.mark.asyncio
async def test_a_missing_mission_id_is_refused_never_held(db, live):
    """A missing required field is a CALLER bug, not a judgement to delegate.
    Holding would ask the operator to approve an action nobody can describe."""
    await _grant(db)
    d = await _check(db, mission_id="", element_name="Save")
    assert d.allow is False
    assert d.reason == "no_mission_id"
    assert d.request_id is None, "a refusal must not queue an approval row"


@pytest.mark.asyncio
async def test_a_nonpositive_process_id_is_refused(db, live):
    """`bool` is an int subclass, so `True` would otherwise arrive as pid 1 —
    a real pid on every Linux box."""
    await _grant(db)
    for pid in (0, -1, True):
        d = await _check(db, process_id=pid, element_name="Save")
        assert d.allow is False, pid
        assert d.reason == "malformed_action:process_id", pid


# ═══════════════ shadow must not be a hand-copy of live ══════════════════


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario",
    [
        "granted_standard", "granted_identity", "granted_financial",
        "no_grant", "wrong_window", "wrong_mission", "denied_cell",
        # The scenario that caught a REAL divergence: a liveness bar was added
        # on the live path only, outside `_verdict`. Every case above uses a
        # fresh grant, so all seven agreed while shadow reported would_allow on
        # a grant live refused as expired. A parity test whose scenarios never
        # exercise a bar cannot see that bar being forked.
        "lapsed_during_authorization",
    ],
)
async def test_shadow_reports_exactly_what_live_decides(db, monkeypatch, scenario):
    """The deliverable of the de-duplication, asserted as an EQUALITY.

    Shadow used to recompute the live verdict by hand — three terms kept in
    step by whoever edited the gate next. Shadow is the mode installs actually
    run, so it is the only thing reporting what live WOULD do; a copy that
    silently disagrees is worse than no shadow at all.

    Parametrised so a bar added to one path and not the other fails here rather
    than in production."""
    monkeypatch.setattr(dg, "grant_ttl_minutes", lambda: 30)
    monkeypatch.setattr(dg, "action_ttl_seconds", lambda: 30)

    kw: dict = {"element_name": "Text Area", "control_type": "Edit"}
    if scenario != "no_grant":
        await _grant(db)
    if scenario == "granted_identity":
        kw["element_name"] = "Delete"
    elif scenario == "granted_financial":
        kw |= {"element_name": "Card number", "control_type": "Edit"}
    elif scenario == "wrong_window":
        kw["window_handle"] = "0xNOPE"
    elif scenario == "wrong_mission":
        kw["mission_id"] = "mis-other"
    elif scenario == "lapsed_during_authorization":
        # ~0.8s of life against a 1-minute TTL, expired by a slow cell write.
        monkeypatch.setattr(dg, "grant_ttl_minutes", lambda: 1)
        await db.execute(
            "UPDATE approval_requests SET resolved_at = ? WHERE action_type = ?",
            ((datetime.now(UTC) - timedelta(seconds=59.2)).isoformat(),
             DESKTOP_GATE_ACTION_TYPE),
        )
        await db.commit()
        real_cc = dg.DesktopTakeoverGate._classify_cell

        async def _slow(self, domain, verb, risk, now):
            import asyncio

            await asyncio.sleep(1.5)
            return await real_cc(self, domain, verb, risk, now)

        monkeypatch.setattr(dg.DesktopTakeoverGate, "_classify_cell", _slow)
    elif scenario == "denied_cell":
        # CLASSIFY first: DENY_PERMANENT is illegal from NOT_DETERMINED, so a
        # cell has to exist before the owner can stand on it.
        for event in (CellEvent.CLASSIFY, CellEvent.DENY_PERMANENT):
            await cg.apply_event(
                db, domain="desktop", verb="control", risk_class="standard",
                event=event, updated_at=_TS, origin_class="owner",
            )
        await db.commit()

    monkeypatch.setattr(dg, "effective_mode", lambda: "shadow")
    shadow_d = await _check(db, **kw)
    monkeypatch.setattr(dg, "effective_mode", lambda: "live")
    live_d = await _check(db, **kw)

    # BOTH halves. Comparing only the boolean samples the policy rather than
    # pinning it: every non-allow outcome collapses to False, so a bar added on
    # one path that turns an allow into a HOLD rather than a REFUSAL — the same
    # fork this test exists to catch, one notch over — would read as agreement.
    assert (shadow_d.would_allow, shadow_d.would_reason) == (live_d.allow, live_d.reason), (
        f"{scenario}: shadow says would_allow={shadow_d.would_allow} "
        f"({shadow_d.would_reason!r}) but live decided allow={live_d.allow} "
        f"({live_d.reason!r})"
    )


@pytest.mark.asyncio
async def test_shadow_writes_no_approval_row_even_when_live_would_hold(db, shadow):
    """Shadow must not put buttons in front of the owner for a capability that
    cannot act — the one place it deliberately DIVERGES from live."""
    await _grant(db)
    d = await _check(db, element_name="Delete", control_type="Button")
    assert d.would_allow is False
    assert d.request_id is None
    rows = await ar.list_pending(db)
    assert [r for r in rows if r["action_type"] == DESKTOP_GATE_ACTION_TYPE] == []


# ═══════════════════════ the hold row's wire format ══════════════════════


@pytest.mark.asyncio
async def test_a_hold_row_carries_a_KIND_not_the_action_type(db, live):
    """`_hold` wrote DESKTOP_GATE_ACTION_TYPE into the `kind` field — the
    action_type where every reader expects a kind. The two strings differ, so
    the row was well-formed JSON carrying a value no lookup could ever match:
    the hold path was WRITE-ONLY."""
    import json

    await _grant(db)
    d = await _check(db, element_name="Delete", control_type="Button")
    assert d.reason == "held" and d.request_id

    row = await ar.get_by_id(db, d.request_id)
    ctx = json.loads(row["context"])
    assert ctx["kind"] == dg.DESKTOP_HOLD_KIND
    assert ctx["kind"] != DESKTOP_GATE_ACTION_TYPE, "a kind, not the action_type"
    assert ctx["kind"] != dg.SESSION_GRANT_KIND, "and never mistakable for a grant"


@pytest.mark.asyncio
async def test_an_unknown_grant_version_is_refused(db, live):
    """A blob written by a different version of the consent path is one whose
    field meanings are not established. Reading it optimistically is how a
    field gets carried without being compared."""
    import json

    rid = await _grant(db)
    ctx = build_session_grant_context(
        session_id=_SESSION, mission_id=_MISSION_ID, mission="m",
        window_handle=_HANDLE, process_id=_PID, window_nonce=_NONCE, window_title=_WINDOW,
    )
    ctx["version"] = dg.SESSION_GRANT_VERSION + 1
    await db.execute(
        "UPDATE approval_requests SET context = ? WHERE id = ?", (json.dumps(ctx), rid)
    )
    await db.commit()
    d = await _check(db, element_name="Text Area")
    assert d.allow is False
    assert d.reason == "grant_malformed"


# ══════════════════════════ money, by shape ══════════════════════════════


def test_a_letter_heavy_IBAN_is_financial_in_every_spelling():
    """The digit-run card pattern already catches IBANs with long digit runs,
    in any spelling. The gap is the letter-heavy ones, whose BBAN breaks the
    run below the 13-digit floor — measured at 2 of 24 specimen spellings."""
    for spelling in (
        "MT84MALT011000012345MTLCAST001S",
        "MT84 MALT 0110 0001 2345 MTLC AST0 01S",
        "mt84 malt 0110 0001 2345 mtlc ast0 01s",
    ):
        c = _classify(element_name="Field", control_type="Edit", text=spelling)
        assert str(c.risk_class) == "financial", spelling


def test_a_commit_hash_is_not_a_bank_transfer():
    """The negative control, and the reason the IBAN test is a CHECKSUM rather
    than a shape. `de`, `be`, `ad`, `ae`, `ba` and `ee` are all real IBAN
    country codes AND valid leading hex pairs, so a country-code alternation
    alone held roughly 1 git SHA in 110. A gate that holds on commit hashes
    teaches the operator to approve without reading."""
    # Chosen to contain NO run of 13+ consecutive digits, so the digit-run card
    # pattern cannot be what decides — otherwise this measures that pattern
    # instead of the IBAN checksum it is named for. ("ee99887766554433221100aa"
    # was here and does hold, correctly: it carries a 20-digit run, and the
    # card pattern is supposed to catch those.)
    for token in (
        "de12ab34cd56ef7890ab", "ab12cdef0123456789abcdef01",
        "be01234567890abcdef12", "eeff00aa11bb22cc33dd44ee",
    ):
        c = _classify(element_name="Field", control_type="Edit", text=token)
        assert str(c.risk_class) == "standard", token


# ═══════════ findings from the adversarial review, each locked ═══════════


@pytest.mark.parametrize(
    "title",
    [
        "Doc AB12CDEFGHKJKLMNOP.txt",   # U+212A KELVIN SIGN
        "Kayit AB12CDEFGHİJKLMNOP",     # U+0130 LATIN CAPITAL I WITH DOT
    ],
)
def test_unicode_in_a_window_title_does_not_raise(title):
    """`re.IGNORECASE` without `re.ASCII` let `[A-Z]` match codepoints that
    case-fold to ASCII. Both of these are `isalpha()`, both survive `.upper()`,
    and both make `int(ch, 36)` raise inside the IBAN checksum — an exception
    escaping `classify_desktop_action` and `check()` entirely.

    The text is a WINDOW TITLE, which this module's threat model treats as
    hostile, so a crash here is reachable from the screen. Enumerated over all
    0x110000 codepoints, exactly these two break the consumer."""
    c = _classify(window_title=title, element_name="Text Area", control_type="Edit")
    assert str(c.risk_class) == "standard"


def test_a_chord_with_a_blank_segment_is_unreadable_not_shorter():
    """Normalization feeds an ALLOWLIST, so every collapse runs toward "inert".
    Dropping a blank segment mapped `ctrl+a+<space>` onto the allowlisted
    `ctrl+a`, and a literal " " is how pyautogui's hotkey() spells space —
    which is deliberately off the list because it activates whatever has focus."""
    from genesis.autonomy.classification import key_chord_risk, normalize_key_chord

    assert str(key_chord_risk("ctrl+a")) == "standard", "control: the real chord"
    for bad in ("ctrl+a+", "ctrl+ +a", "ctrl+\xa0+a", "ctrl++a"):
        # Assert the SENTINEL, not merely "!= ctrl+a". Without the explicit
        # guard a blank segment still yields "ctrl++a", which misses the
        # allowlist for an incidental reason — the join happens to insert a
        # second "+". A test that only checks the risk therefore passes with
        # the guard deleted, and the next person to re-add a tidy
        # `[p for p in parts if p]` filter silently reopens the collapse.
        assert normalize_key_chord(bad) == "\x00unreadable", bad
        assert str(key_chord_risk(bad)) == "identity", bad


@pytest.mark.asyncio
async def test_cut_holds_because_it_moves_files_like_delete_does(db, live):
    """`ctrl+x` inherits the argument that keeps `delete` off the inert list.
    In a file manager `ctrl+a` selects every item, `ctrl+x` marks them for a
    MOVE and `ctrl+v` completes it — and a KEY action produces no element name
    for the classifier to read. Copy and paste stay inert; they are additive."""
    await _grant(db)
    cut = await _check(db, operation=DesktopOperation.KEY, key_chord="ctrl+x",
                       element_name="", control_type="")
    assert cut.allow is False and cut.reason == "held"
    for chord in ("ctrl+c", "ctrl+z"):
        d = await _check(db, operation=DesktopOperation.KEY, key_chord=chord,
                         element_name="", control_type="")
        assert d.allow is True, f"control: {chord} must stay ordinary"


@pytest.mark.asyncio
async def test_a_string_process_id_still_matches_its_own_grant(db, live):
    """`parse` stores an int pid and a stripped handle; the action side is
    whatever the caller built. An actuator returning `"4312"` from JSON or a
    PowerShell bridge passes validation and would then fail `authorises`
    forever — reported as `grant_scope_mismatch`, i.e. "that grant was for a
    different window", for the window it was actually granted for. Fail-closed,
    and the one field mismatch the reason string cannot tell you about."""
    await _grant(db, window_handle="0xABC1", process_id=4312)
    d = await _check(db, window_handle="  0xABC1  ", process_id="4312",
                     element_name="Text Area", control_type="Edit")
    assert d.allow is True, f"refused as {d.reason}"


@pytest.mark.asyncio
async def test_a_grant_for_a_DIFFERENT_pid_is_still_refused(db, live):
    """The control for the normalization above: normalizing must not make two
    different pids compare equal. A handle is reused once its window closes,
    which is why the pair is the identity."""
    await _grant(db, window_handle="0xABC1", process_id=4312)
    d = await _check(db, window_handle="0xABC1", process_id=9999,
                     element_name="Text Area", control_type="Edit")
    assert d.allow is False
    assert d.reason == "grant_window_mismatch"


def test_a_plural_money_label_is_still_a_money_label():
    """`\\bpayment\\b` does not match "Payments" — the trailing s defeats the
    word boundary, and "Payments" is what a real banking window is called.
    Measured: a window titled "Chase - Payments" classified STANDARD while
    "Online Banking" did not."""
    for title in ("Chase - Payments", "Invoices", "Card numbers"):
        c = _classify(window_title=title, element_name="Field", control_type="Edit")
        assert str(c.risk_class) == "financial", title
    for title in ("Payment", "Invoice", "Card number"):
        c = _classify(window_title=title, element_name="Field", control_type="Edit")
        assert str(c.risk_class) == "financial", f"control (singular): {title}"


# ═══════════ findings from the security review, each locked ══════════════


@pytest.mark.asyncio
async def test_paste_cannot_be_used_to_fill_a_focused_password_box(db, live):
    """The module claims absolutely that nothing types into a password box.

    A KEY action has NO resolved target by construction, so `is_password` is
    always False for a keypress and the secret-field refusal has nothing to
    match. `tab` is inert, so the loop can move focus into a password field;
    with `ctrl+v` inert too, the clipboard went in and the gate called it
    ordinary input. That is not the stated hostile-screen limit — there is no
    control name to have lied about.

    Measured before the fix: allow=True, reason=session_grant."""
    await _grant(db)
    d = await _check(db, operation=DesktopOperation.KEY, key_chord="ctrl+v",
                     element_name="", control_type="",
                     window_title="Untitled - Notepad")
    assert d.allow is False, "paste must not be an allowlisted chord"
    assert d.reason == "held"


@pytest.mark.asyncio
async def test_chords_that_cannot_insert_stay_ordinary(db, live):
    """The control for the line the allowlist draws. It is INSERTION, not
    mutation: backspace and undo can disturb a focused field but cannot put
    chosen content into one, and holding them would break ordinary typing."""
    await _grant(db)
    for chord in ("ctrl+c", "backspace", "ctrl+z", "ctrl+y", "tab", "ctrl+a"):
        d = await _check(db, operation=DesktopOperation.KEY, key_chord=chord,
                         element_name="", control_type="",
                         window_title="Untitled - Notepad")
        assert d.allow is True, f"{chord} must stay ordinary"


@pytest.mark.asyncio
async def test_screen_text_cannot_forge_the_consent_card_structure(db, live):
    """`_display` strips control characters, so a newline cannot be forged —
    but the card quoted its fields INLINE, and an apostrophe is purely
    printable. An element named `Cancel' in 'Notepad` produced a card that read
    as well-formed and named the wrong window, demoting the real one to a
    trailing fragment.

    The fix is structural: one field per LINE, using the delimiter `_display`
    guarantees cannot appear. Stripping quotes instead would corrupt every
    legitimate title ("Bob's Document")."""
    import json

    await _grant(db)
    d = await _check(db, element_name="Delete' in 'Notepad - untitled",
                     control_type="Button", window_title="Evil Bank - Payments")
    assert d.reason == "held" and d.request_id
    row = await ar.get_by_id(db, d.request_id)

    lines = row["description"].split("\n")
    control_lines = [ln for ln in lines if ln.startswith("Control: ")]
    window_lines = [ln for ln in lines if ln.startswith("Window: ")]
    assert len(control_lines) == 1, lines
    assert len(window_lines) == 1, "screen text forged a second Window line"
    assert window_lines[0] == "Window: Evil Bank - Payments", (
        "the REAL window must be what the Window line names"
    )
    # And a legitimate apostrophe survives — the fix must not sanitise titles.
    assert "Delete' in 'Notepad - untitled" in control_lines[0]

    ctx = json.loads(row["context"])
    for field in ("mission_id", "window_handle", "process_id", "operation"):
        assert field in ctx, f"a hold row must be bindable to what it was about: {field}"


@pytest.mark.asyncio
async def test_a_mission_mismatch_does_not_report_a_window_mismatch(db, live):
    """This module's own standard: a refusal reason must not name the wrong
    bar. Folding session, mission and window into one boolean reported
    `grant_scope_mismatch` — "that grant was for a different window" — for a
    grant whose window was exactly right."""
    await _grant(db, mission_id="mis-tidy")
    d = await _check(db, mission_id="mis-other", element_name="Text Area",
                     control_type="Edit")
    assert d.allow is False
    assert d.reason == "grant_mission_mismatch"

    # The MATCHING mission, so the window bar is what fails. Without it this
    # probe carries a mission mismatch too — which is checked first — and would
    # report the reason it is meant to be distinguishing from.
    other = await _check(db, mission_id="mis-tidy", window_handle="0xNOPE",
                         element_name="Text Area", control_type="Edit")
    assert other.reason == "grant_window_mismatch", "and the window bar keeps its own"


def test_an_oversized_screen_field_is_bounded_and_says_so(caplog):
    """Classification is synchronous work inside an async gate, measured at
    ~1.2 ms/KB. An accessibility tree exposing a document as an element name
    would block the event loop for minutes, once per action.

    The bound is LOSSY, which is why it logs: content past it is not examined,
    and a silent cut would make a card number past the boundary read as absent."""
    import logging

    with caplog.at_level(logging.WARNING):
        c = _classify(element_name="A" * 50_000, control_type="Edit")
    assert str(c.risk_class) == "standard"
    assert any("bounded element_name" in r.getMessage() for r in caplog.records), caplog.text


def test_desktop_rows_never_reach_the_unified_comms_feed():
    """A THIRD surface renders pending approvals, and it had no exclusion.

    The desktop carve-out is maintained by ENUMERATION — there is no allowlist
    that catches a new reader — so this was found by listing every caller of
    `approval_requests.list_pending` rather than by fixing the one that was
    reported. The other readers are safe for their own reasons:
    `hydrate_delivery_map` matches on a `delivery_id` a desktop row does not
    carry. The morning report is NOT one of them — it renders the oldest five
    descriptions, not just a count. It offers no button, so it is not an
    approval surface; but "it only counts" was WRONG, and the correction is
    recorded here as well as in the code, because a false claim in permanent
    record is what the next reader builds on. What bounds it there is the row's
    own lifetime, which is why desktop holds now carry a TTL.

    Same hazard as the approvals queue: the feed renders a pending row as a
    generic approval card, and a card that cannot say it is handing over the
    operator's keyboard must not be the thing that asks."""
    from unittest.mock import AsyncMock, MagicMock, patch

    from flask import Flask

    from genesis.dashboard.api import blueprint

    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True

    rows = [
        {"id": "desk", "action_type": DESKTOP_GATE_ACTION_TYPE, "context": "{}",
         "description": "Desktop control", "created_at": _TS},
        {"id": "cli", "action_type": "autonomous_cli_fallback", "context": "{}",
         "description": "cli action", "created_at": _TS},
    ]
    mock_rt = MagicMock()
    mock_rt.is_bootstrapped = True

    with (
        patch("genesis.runtime.GenesisRuntime") as MockRT,
        patch("genesis.db.crud.approval_requests.list_pending",
              AsyncMock(return_value=rows)),
        patch("genesis.db.crud.ego.list_pending_proposals",
              AsyncMock(return_value=[])),
    ):
        MockRT.instance.return_value = mock_rt
        resp = app.test_client().get("/api/genesis/comms")

    assert resp.status_code == 200, resp.get_data(as_text=True)
    ids = {r["id"] for r in resp.get_json()["pending_approvals"]}
    assert "cli" in ids, "positive control: the feed DID render approvals"
    assert "desk" not in ids


def test_a_padded_card_number_does_not_walk_past_the_digit_run(db=None):
    """`{13,19}` with a trailing `(?!\\d)` did not mean "13 to 19 digits" — it
    meant a run of 13-19 with no digit after it, so a run of TWENTY matched
    nothing and classified STANDARD. One padding digit was the whole evasion.

    Measured cost of removing the cap: +0.10 percentage points on 20,000 random
    git SHAs (2.54% -> 2.63%), which is 19 extra holds to close a trivial
    bypass."""
    for n in (13, 19, 20, 25, 40):
        c = _classify(operation=DesktopOperation.TYPE, element_name="Field",
                      control_type="Edit", text="4" * n)
        assert str(c.risk_class) == "financial", f"{n} digits"
    # The control: below the floor is still ordinary, or every long number holds.
    c = _classify(operation=DesktopOperation.TYPE, element_name="Field",
                  control_type="Edit", text="4" * 12)
    assert str(c.risk_class) == "standard"


def test_a_pin_verb_is_not_a_secret_field():
    """A bare case-insensitive `\\bpin\\b` made "Pin to taskbar" and "Pin to
    Start" — ordinary Windows shell actions — match as SECRET FIELDS.

    That is the worst false positive this gate can produce: a secret-field
    match is a REFUSAL with no approval path, so the operator cannot proceed at
    all, and the reason names something the control has nothing to do with.
    The credential senses are matched explicitly instead."""
    for verb in ("Pin to taskbar", "Pin to Start", "Unpin from Start",
                 "Pin this to Quick access"):
        assert _classify(element_name=verb).is_password is False, verb
    for credential in ("PIN", "Pin code", "Enter your PIN", "New PIN",
                       "pin number", "Confirm PIN"):
        assert _classify(element_name=credential, control_type="Edit").is_password is True, (
            credential
        )


# ═══════════ Codex round 4, each locked ══════════════════════════════════


@pytest.mark.asyncio
async def test_a_recycled_window_handle_does_not_inherit_the_grant(db, live):
    """`(handle, pid)` is not unique OVER TIME, which is the same defect the
    title had in space.

    A Windows HWND is valid for a window's lifetime and is then RECYCLED, and
    the pid does not save it: a browser or editor keeps one process alive across
    many windows, so a newly created window can receive a closed window's handle
    while the 30-minute grant is still live. Under a handle+pid bar alone, that
    new window — which the operator never saw named — inherits consent.

    The nonce is the actuator's guarantee that the window occupying the handle
    is the same one. The gate cannot observe window lifetimes, so it REQUIRES
    the information rather than inferring it."""
    await _grant(db, window_handle="0xRECYC", process_id=7000,
                 window_nonce="gen-1")

    same = await _check(db, window_handle="0xRECYC", process_id=7000,
                        window_nonce="gen-1", element_name="Text Area",
                        control_type="Edit")
    assert same.allow is True, "positive control: the granted window still works"

    # Same handle, same LIVE process, new window.
    recycled = await _check(db, window_handle="0xRECYC", process_id=7000,
                            window_nonce="gen-2", element_name="Text Area",
                            control_type="Edit")
    assert recycled.allow is False
    assert recycled.reason == "grant_window_mismatch"


@pytest.mark.asyncio
async def test_a_missing_window_nonce_is_a_malformed_call(db, live):
    """Required, like every other identity field: absent information must be a
    caller error rather than an empty string that compares equal to another
    empty string."""
    await _grant(db)
    d = await _check(db, window_nonce="", element_name="Text Area")
    assert d.allow is False
    assert d.reason == "malformed_action:window_nonce"


@pytest.mark.asyncio
async def test_a_non_finite_process_id_is_refused_not_raised(db, live):
    """`int()` is not a validator. It raises OverflowError on infinity — which
    neither conversion site caught — so a payload carrying `1e999` (valid JSON
    that Python and SQLite both parse as inf) crashed `check()` instead of being
    refused. `nan` is the same shape.

    And it TRUNCATES 4312.7 to 4312, silently inventing a real pid from a
    malformed one, so the grant then matches a process nobody named. Refusing
    is the only honest answer to both."""
    await _grant(db)
    for pid in (float("inf"), float("-inf"), float("nan"), 4312.7, -0.5):
        d = await _check(db, process_id=pid, element_name="Text Area")
        assert d.allow is False, pid
        assert d.reason == "malformed_action:process_id", pid

    # The control: a float that IS integral is a legitimate spelling of a pid.
    ok = await _check(db, process_id=float(_PID), element_name="Text Area",
                      control_type="Edit")
    assert ok.allow is True, "an integral float is still a pid"


@pytest.mark.asyncio
async def test_a_stored_grant_with_a_non_finite_pid_does_not_crash_lookup(db, live):
    """The same incomplete conversion existed in `SessionGrant.parse`, so ONE
    malformed approved row could break the lookup for every action in the
    session — including actions whose own grant was fine.

    A FRACTIONAL pid, not infinity, and the difference matters. The review named
    `1e999`, but `json.dumps(inf)` emits the bare token `Infinity`, which is not
    valid JSON — and the SQL lookup guards on `json_valid`, so such a row is
    filtered out before `parse` ever sees it. `4312.7` IS valid JSON, does reach
    parse, and was silently truncated to 4312 there: a malformed row inventing a
    real pid, which then matches a process nobody named. The reported mechanism
    was wrong; the underlying defect was real."""
    import json

    rid = await _grant(db)
    ctx = build_session_grant_context(
        session_id=_SESSION, mission_id=_MISSION_ID, mission="m",
        window_handle=_HANDLE, process_id=_PID, window_nonce=_NONCE,
        window_title=_WINDOW,
    )
    ctx["process_id"] = 4312.7
    await db.execute(
        "UPDATE approval_requests SET context = ? WHERE id = ?", (json.dumps(ctx), rid)
    )
    await db.commit()

    d = await _check(db, element_name="Text Area", control_type="Edit")
    assert d.allow is False
    assert d.reason == "grant_malformed", "refused, not raised"


# ═══════════ _verdict, directly — the policy with nothing around it ═══════════
#
# `_verdict` holds the ENTIRE allow/hold/refuse policy and had no direct test:
# every assertion about it arrived through check(), a DB fixture and the real
# clock. That is why its liveness bar could only be tested with a sleep, and why
# that test can go quiet on a loaded box (see the vacuity guard above).
#
# It is pure — no clock, no config, no DB — so the whole policy is a table.


def _classification(risk: RiskClass, *, is_password: bool = False):
    return DesktopActionClassification(
        domain="desktop", verb="control", risk_class=risk,
        sub_class="input", is_password=is_password,
        identity_bar=risk is not RiskClass.STANDARD,
        action_class=ActionClass.REVERSIBLE,
    )


def _grant_resolved_at(dt):
    return dg.SessionGrant(
        row_id="r1", session_id="s1", mission_id="m1", window_handle="0xA1",
        process_id=4312, window_nonce="n1", window_title="W",
        mission="do a thing", resolved_by="telegram:owner", resolved_at=dt,
    )


_T0 = datetime(2026, 9, 9, 12, 0, 0, tzinfo=UTC)
_TTL = timedelta(minutes=30)


@pytest.mark.parametrize(
    ("label", "grant", "grant_reason", "cell_state", "risk", "now", "expected"),
    [
        ("no grant refuses with the selection reason",
         None, "no_session_grant", CellState.NOT_DETERMINED, RiskClass.STANDARD, _T0,
         (False, "no_session_grant")),
        ("a live grant on a STANDARD action allows",
         _grant_resolved_at(_T0), "", CellState.NOT_DETERMINED, RiskClass.STANDARD, _T0,
         (True, "session_grant")),
        ("expired by one second refuses",
         _grant_resolved_at(_T0), "", CellState.NOT_DETERMINED, RiskClass.STANDARD,
         _T0 + _TTL + timedelta(seconds=1), (False, "grant_expired")),
        ("exactly at the TTL is still live — the bound is inclusive",
         _grant_resolved_at(_T0), "", CellState.NOT_DETERMINED, RiskClass.STANDARD,
         _T0 + _TTL, (True, "session_grant")),
        ("a future-dated grant refuses, never mints a permanent one",
         _grant_resolved_at(_T0), "", CellState.NOT_DETERMINED, RiskClass.STANDARD,
         _T0 - timedelta(seconds=1), (False, "grant_expired")),
        ("an un-ageable grant is treated as expired",
         _grant_resolved_at(None), "", CellState.NOT_DETERMINED, RiskClass.STANDARD, _T0,
         (False, "grant_expired")),
        ("DENIED_PERMANENT outranks a live grant",
         _grant_resolved_at(_T0), "", CellState.DENIED_PERMANENT, RiskClass.STANDARD, _T0,
         (False, "denied_permanent")),
        ("above STANDARD holds even under a live grant",
         _grant_resolved_at(_T0), "", CellState.NOT_DETERMINED, RiskClass.IDENTITY, _T0,
         (False, "held")),
        ("financial holds too",
         _grant_resolved_at(_T0), "", CellState.NOT_DETERMINED, RiskClass.FINANCIAL, _T0,
         (False, "held")),
        # ORDER matters: an expired grant on a DENIED cell must name the grant,
        # because liveness is checked before the cell. Pins the sequence, not
        # just the set, of bars.
        ("expiry is reported before the cell verdict",
         _grant_resolved_at(_T0), "", CellState.DENIED_PERMANENT, RiskClass.STANDARD,
         _T0 + _TTL + timedelta(seconds=1), (False, "grant_expired")),
    ],
)
def test_verdict_is_a_pure_table(label, grant, grant_reason, cell_state, risk, now, expected):
    """The whole policy, with no DB, no sleep and no race.

    This is the guard the liveness fix actually needs: the integration test for
    it depends on wall clock and can therefore stop testing without failing.
    This one cannot."""
    assert dg._verdict(
        classification=_classification(risk), grant=grant, grant_reason=grant_reason,
        cell_state=cell_state, now=now, grant_ttl=_TTL,
    ) == expected, label


def test_verdict_reads_no_clock_of_its_own():
    """Purity, asserted rather than documented. The same inputs must give the
    same answer regardless of when it is called — which is what makes the table
    above meaningful, and what a re-added `datetime.now()` inside `_verdict`
    would silently break."""
    kw = dict(
        classification=_classification(RiskClass.STANDARD),
        grant=_grant_resolved_at(_T0), grant_reason="",
        cell_state=CellState.NOT_DETERMINED, grant_ttl=_TTL,
    )
    # A `now` far in the past and far in the future, both outside the TTL.
    assert dg._verdict(now=_T0 - timedelta(days=365), **kw) == (False, "grant_expired")
    assert dg._verdict(now=_T0 + timedelta(days=365), **kw) == (False, "grant_expired")
    # And inside it, twice, with real time passing between the calls.
    assert dg._verdict(now=_T0, **kw) == (True, "session_grant")
    assert dg._verdict(now=_T0, **kw) == (True, "session_grant")


# ═══ Codex round 5 — ONE class, enumerated rather than patched instance-wise ═══
#
# Three of the four findings were the same shape: an externally-supplied value
# coerced with a guard that does not cover every way the coercion fails. The pid
# fix in the previous round closed ONE member and I did not enumerate the rest,
# which is why this round found more. Enumerating `int(` / `len(` / datetime
# conversion across the three files found SIX members — two of which were not
# reported, and are covered below.


@pytest.mark.asyncio
async def test_a_grant_that_lapses_during_authorization_does_not_authorize(db, live, monkeypatch):
    """`_classify_cell` is a SQLite write between selecting the grant and acting
    on it. A grant with seconds left can lapse across that await, after which
    the action would still be allowed AND stamped with a fresh device TTL —
    extending an expired consent rather than ending it. Small window,
    fail-OPEN direction, which is the combination that does not announce
    itself."""
    import asyncio

    # A ONE-MINUTE TTL and a grant with ~0.8s of life left, so real elapsed time
    # is what expires it. Mutating the stored row would not work: `resolved_at`
    # is read into the frozen SessionGrant at parse time, so the re-check would
    # compare the clock against the same value it already saw. The thing under
    # test is the CLOCK moving, so the clock has to move.
    monkeypatch.setattr(dg, "grant_ttl_minutes", lambda: 1)
    rid = await _grant(db)
    await db.execute(
        "UPDATE approval_requests SET resolved_at = ? WHERE id = ?",
        ((datetime.now(UTC) - timedelta(seconds=59.2)).isoformat(), rid),
    )
    await db.commit()

    real = dg.DesktopTakeoverGate._classify_cell

    async def _slow(self, domain, verb, risk, now):
        # A slow SQLite write. Bounded and short; it is the elapsed time itself
        # that is under test, not a condition to poll for.
        await asyncio.sleep(1.5)
        return await real(self, domain, verb, risk, now)

    monkeypatch.setattr(dg.DesktopTakeoverGate, "_classify_cell", _slow)

    # This test depends on ~0.8s of real time NOT elapsing before the grant is
    # selected. If it does, the grant is already expired at SELECTION, the
    # re-check under test is never reached, and every assertion below still
    # holds — the test would go quiet while the mechanism was gone. So record
    # what `is_live` actually saw and fail LOUDLY on that instead.
    calls: list[bool] = []
    real_is_live = dg.SessionGrant.is_live

    def _recording(self, ttl, *, now=None):
        result = real_is_live(self, ttl, now=now)
        calls.append(result)
        return result

    monkeypatch.setattr(dg.SessionGrant, "is_live", _recording)

    d = await _check(db, element_name="Text Area", control_type="Edit")

    assert calls, "is_live was never called — the grant never reached a liveness bar"
    assert calls[0] is True, (
        "VACUOUS: the grant had already expired at SELECTION, so the re-check "
        "under test never ran. This is a slow/contended box, not a regression — "
        "but the assertions below would have passed with the mechanism deleted."
    )
    assert len(calls) >= 2, "the re-check inside _verdict was never reached"
    assert calls[-1] is False, "the re-check should have found the grant expired"

    assert d.allow is False, "an expired grant must not authorize, however it expired"
    assert d.reason == "grant_expired"
    assert d.expires_at is None, "and must not receive a fresh device TTL"


def test_an_extreme_offset_timestamp_is_unageable_not_a_crash():
    """`datetime.fromisoformat` accepts "9999-12-31T23:59:59-23:59" and
    `astimezone(UTC)` then raises OverflowError. `_parse_ts` caught only the
    PARSE-time ValueError, so one malformed approved row would take down every
    lookup for that session instead of being the un-ageable grant it is."""
    assert dg._parse_ts("9999-12-31T23:59:59-23:59") is None
    assert dg._parse_ts("0001-01-01T00:00:00+23:59") is None
    # Controls: ordinary timestamps still parse, in both spellings.
    assert dg._parse_ts("2026-06-21T00:00:00+00:00") is not None
    assert dg._parse_ts("2026-06-21T00:00:00Z") is not None
    assert dg._parse_ts("2026-06-21T00:00:00") is not None  # naive -> UTC


@pytest.mark.asyncio
async def test_non_string_screen_metadata_is_refused_not_raised(db, live):
    """Every validation check stringifies its input, so a JSON number arriving
    where a string was declared passed validation and then hit `len()` inside
    the classifier as an int. A malformed external payload must fail CLOSED with
    a reason, not crash the loop."""
    await _grant(db)
    for field in ("window_title", "element_name", "control_type", "text",
                  "window_handle", "window_nonce", "key_chord"):
        d = await _check(db, **{field: 12345})
        assert d.allow is False, field
        assert d.reason == f"malformed_action:{field}", field


def test_the_display_and_classifier_helpers_survive_a_non_string():
    """The second layer, and the two members of this class that were NOT
    reported. `_display` feeds the consent card a human reads, and reached
    `strip_control_chars` as an int; `_bounded` reached `len()` the same way.
    Both are reachable from tests and from whatever calls the classifier after
    PR-3, so neither relies on the gate boundary alone."""
    assert dg._display(12345) == "12345"
    assert dg._display(None) == ""
    from genesis.autonomy.classification import _bounded

    assert _bounded(12345, "element_name") == "12345"
    assert _bounded(None, "element_name") == ""


# ═══ fresh-context audit: the hold blob is a forward commitment to PR-3 ═══


@pytest.mark.asyncio
async def test_the_hold_blob_is_versioned_normalized_and_complete(db, live):
    """The hold context is written NOW for a reader that lands with the consent
    path, which makes it a forward-compatibility commitment. Three things it
    was missing, each the same defect this PR closed elsewhere:

    VERSION — the grant blob refuses an unknown version precisely so a field
    cannot be carried without being compared. The hold blob had none, so the
    future reader would have no way to refuse a blob whose meanings it did not
    establish.

    NORMALIZATION — the read side strips and coerces (see SessionGrant.parse and
    .mismatch), because `4312 == "4312"` is False and fail-closed-but-
    undiagnosable. The write side stored the raw caller value, handing that same
    mismatch to whoever binds on these rows.

    COMPLETENESS — for a KEY action there is no element name, so without the
    chord neither the card nor any later reader can say what is being held."""
    import json

    # The grant carries the CANONICAL identity; the action carries the padded,
    # stringified spelling an actuator would actually hand back. They must match
    # (the read side normalizes both), so the hold is reached and its blob can
    # be inspected.
    await _grant(db, window_handle="0xPAD", process_id=4312, window_nonce="n-pad")
    d = await _check(
        db, operation=DesktopOperation.KEY, key_chord="ctrl+enter",
        element_name="", control_type="",
        window_handle="  0xPAD  ", process_id="4312", window_nonce="  n-pad  ",
    )
    assert d.reason == "held" and d.request_id

    row = await ar.get_by_id(db, d.request_id)
    ctx = json.loads(row["context"])

    assert ctx["version"] == dg.DESKTOP_HOLD_VERSION
    assert ctx["window_handle"] == "0xPAD", "stored as it will be compared"
    assert ctx["process_id"] == 4312, "an int, not the string the caller sent"
    assert ctx["window_nonce"] == "n-pad"
    assert ctx["key_chord"] == "ctrl+enter", "the thing that caused the hold"
    assert "control_type" in ctx
    # ONE fact, ONE name. The risk class is already `cell[2]` (cell_key is
    # literally (domain, verb, str(risk_class))), and a separate "risk_class"
    # key carried the identical string. In a VERSIONED blob a future reader
    # will compare, two spellings of one fact is how a later writer makes them
    # disagree — the same reasoning that put a version on this blob.
    assert ctx["cell"] == ["desktop", "control", "identity"]
    assert "risk_class" not in ctx, "dropped as a duplicate of cell[2]"

    # And the card can name it, which it could not before: a KEY action has no
    # element name, so the card said "an unnamed control" and stopped.
    assert "Key: ctrl+enter" in row["description"]


@pytest.mark.asyncio
async def test_one_malformed_row_does_not_break_the_resume_lookup(db):
    """`find_approved_unconsumed` had the bare `json_extract` this PR's own new
    query documents as measured-fatal, 60 lines away in the same file: an
    unguarded extract raises "malformed JSON" on ONE invalid row and takes the
    whole query with it.

    That query is the awareness loop's resume path, so the same corrupt row that
    would have broken the desktop lookup also broke reflection resume. The class
    was identified, measured and guarded in one member while its only sibling
    sat unfixed in the file being edited."""
    mgr = ApprovalManager(db=db)
    rid = await mgr.request_approval(
        action_type="reflection_resume", action_class="reversible",
        description="resume", context='{"subsystem": "awareness", "policy_id": "p1"}',
        timeout_seconds=None,
    )
    await mgr.resolve(rid, status="approved", resolved_by="telegram:1")
    # A hand-edited / corrupted sibling row, which is all it takes.
    await db.execute(
        "INSERT INTO approval_requests (id, action_type, action_class, description,"
        " context, status, created_at) VALUES (?,?,?,?,?,?,?)",
        ("broken", "other", "reversible", "d", "{not json", "approved", _TS),
    )
    await db.commit()

    found = await ar.find_approved_unconsumed(db, subsystem="awareness", policy_id="p1")
    assert found is not None, "one malformed row must not take down the lookup"
    assert found["id"] == rid


@pytest.mark.asyncio
async def test_the_resume_lookups_24_hour_window_is_actually_24_hours(db):
    """MEASURED, and it was ~48. `resolved_at` is written by Python as
    "2026-09-08T05:22:44.814096+00:00"; `datetime('now','-24 hours')` renders as
    "2026-09-08 21:22:44". The comparison is lexicographic and 'T' (0x54) beats
    ' ' (0x20), so ANY row sharing the threshold's DATE compared greater no
    matter its time of day — a 40-hour-old approval passed a window documented
    as 24 hours, while a 70-hour-old one did not.

    Fail-OPEN on a staleness guard, on the awareness loop's resume path.
    Pre-existing; found while auditing the predicate this PR rewrote."""
    mgr = ApprovalManager(db=db)

    async def _aged(hours: float, policy: str) -> str:
        rid = await mgr.request_approval(
            action_type="reflection_resume", action_class="reversible",
            description="resume", timeout_seconds=None,
            context=f'{{"subsystem": "awareness", "policy_id": "{policy}"}}',
        )
        await mgr.resolve(rid, status="approved", resolved_by="telegram:1")
        await db.execute(
            "UPDATE approval_requests SET resolved_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(hours=hours)).isoformat(), rid),
        )
        await db.commit()
        return rid

    fresh = await _aged(2, "fresh")
    await _aged(40, "stale40")   # the case that used to pass
    await _aged(70, "stale70")   # excluded even before, by the DATE differing

    assert (await ar.find_approved_unconsumed(
        db, subsystem="awareness", policy_id="fresh"))["id"] == fresh, (
        "a 2-hour-old approval is inside the window and must still resume")
    assert await ar.find_approved_unconsumed(
        db, subsystem="awareness", policy_id="stale40") is None, (
        "a 40-hour-old approval is outside a 24-hour window")
    assert await ar.find_approved_unconsumed(
        db, subsystem="awareness", policy_id="stale70") is None


@pytest.mark.asyncio
async def test_a_malformed_row_does_not_break_the_cli_approval_probe(db):
    """The third member of the class, and the reason the enumeration was
    re-scoped: `json_extract` over `approval_requests.context` is a COLUMN-level
    class, not a file-level one. Guarding the two queries in
    `approval_requests.py` left `ego.has_pending_cli_approval` — the probe the
    ego cadence uses to ask whether an autonomous-CLI approval is already
    pending — reading the same column unguarded."""
    from genesis.db.crud import ego as ego_crud

    mgr = ApprovalManager(db=db)
    rid = await mgr.request_approval(
        action_type="autonomous_cli_fallback", action_class="reversible",
        description="dispatch", timeout_seconds=None,
        context='{"policy_id": "cadence-1"}',
    )
    assert rid
    await db.execute(
        "INSERT INTO approval_requests (id, action_type, action_class, description,"
        " context, status, created_at) VALUES (?,?,?,?,?,?,?)",
        ("broken-cli", "autonomous_cli_fallback", "reversible", "d",
         "{not json", "pending", _TS),
    )
    await db.commit()

    assert await ego_crud.has_pending_cli_approval(db, "cadence-1") is True
    assert await ego_crud.has_pending_cli_approval(db, "no-such-policy") is False


@pytest.mark.asyncio
async def test_a_lone_surrogate_in_screen_text_does_not_crash_the_gate(db, live):
    """Hostile screen text must not be able to take out the authorization gate.

    `strip_control_chars` is derived from the Unicode database, and a lone
    UTF-16 surrogate is not a control character, so it passed through untouched.
    `json.loads` of an escaped surrogate produces one — an actuator relaying
    screen metadata can hand us one — and encoding it as UTF-8 raises
    UnicodeEncodeError, which is precisely what the DB driver does when binding
    the description. The gate CRASHED on the hold path instead of recording the
    hold: refusing nothing, allowing nothing, taking the caller with it."""
    await _grant(db)
    d = await _check(db, element_name="Delete\ud800", control_type="Button")
    assert d.reason == "held" and d.request_id, "the hold must still be RECORDED"

    row = await ar.get_by_id(db, d.request_id)
    row["description"].encode("utf-8")  # the operation that used to raise
    assert "Delete" in row["description"], "the rest of the name still reaches the operator"


@pytest.mark.asyncio
async def test_the_device_deadline_starts_when_the_gate_STOPS_working(db, live, monkeypatch):
    """`expires_at` was computed before `_emit_allowed`, which awaits its event
    listeners INLINE. A listener slower than the action TTL therefore handed the
    device a deadline already in the past, and the device correctly refused an
    action the gate correctly allowed."""
    import asyncio

    await _grant(db)
    monkeypatch.setattr(dg, "action_ttl_seconds", lambda: 1)

    real_emit = dg.DesktopTakeoverGate._emit_allowed

    async def _slow(self, *a, **kw):
        await asyncio.sleep(1.4)  # longer than the whole TTL
        return await real_emit(self, *a, **kw)

    monkeypatch.setattr(dg.DesktopTakeoverGate, "_emit_allowed", _slow)

    d = await _check(db, element_name="Text Area", control_type="Edit")
    assert d.allow is True and d.expires_at

    remaining = (datetime.fromisoformat(d.expires_at) - datetime.now(UTC)).total_seconds()
    assert remaining > 0, (
        f"the device deadline was already {-remaining:.1f}s in the past on return — "
        "the action would be refused at the machine for time it was never given"
    )


@pytest.mark.asyncio
async def test_the_kill_switch_reads_no_config_at_all(db, monkeypatch):
    """`effective_mode()` checks the env kill switch BEFORE any YAML, so an
    unparseable config is not a way around the stop. Reading the TTL at the top
    of `check()` briefly put a base+overlay reload in FRONT of the off-return,
    which made the emergency disable wait on the very files it exists to bypass.

    Asserting "the kill switch wins" would NOT catch that — it wins either way.
    The property is that the off path touches no config, so the test makes any
    config read explode and requires the refusal anyway."""
    monkeypatch.setenv(dtc.DISABLE_ENV, "1")

    def _explode():
        raise AssertionError("the off path must not read config")

    monkeypatch.setattr(dg, "grant_ttl_minutes", _explode)
    monkeypatch.setattr(dg, "action_ttl_seconds", _explode)

    d = await _check(db, element_name="Save", control_type="Button")
    assert d.allow is False
    assert d.reason == "not_armed"


@pytest.mark.asyncio
async def test_a_nonboolean_password_flag_is_refused(db, live):
    """`is_password` is a REFUSAL flag with no approval path, so it gets the same
    strictness as the seven textual fields and the pid. It was the one field on
    the object with no validation: a JSON `0`/`None`/`""` from a future transport
    would be accepted, `bool(...)` would read it False, and a generically-named
    password control would classify STANDARD and pass under a live grant."""
    await _grant(db)
    for flag in (0, 1, None, "", "true"):
        d = await _check(db, element_name="Password", control_type="Edit", is_password=flag)
        assert d.allow is False, flag
        assert d.reason == "malformed_action:is_password", flag

    # CONTROL: real booleans still work, both ways round.
    d = await _check(db, element_name="Text Area", control_type="Edit", is_password=False)
    assert d.allow is True and d.reason == "session_grant"
    d = await _check(db, element_name="Password", control_type="Edit", is_password=True)
    assert d.allow is False and d.reason == "password_field"


@pytest.mark.asyncio
async def test_an_object_MISSING_a_textual_field_is_refused_not_crashed(db, live):
    """The type loop defaulted `getattr(action, field, "")`, so an object with
    the attribute ABSENT satisfied `isinstance(..., str)` and sailed through —
    then hit `_bounded(action.text)` and raised AttributeError out of `check()`.
    That is the exact crash the block's own comment forbids, re-opened for one
    input shape by the default it was given.

    Unreachable while the caller is the frozen `DesktopAction`, which always has
    all ten fields. It becomes reachable the moment PR-3 hands the gate an
    adapter or a duck-typed object — the same reasoning that justified the
    second coercion layer inside `_bounded`."""
    await _grant(db)

    class _PartialAction:
        """Everything a DesktopAction has, except `text`."""

        operation = DesktopOperation.CLICK
        window_handle = "0xAAA1"
        process_id = 4312
        window_nonce = "nonce-1"
        window_title = "Untitled - Notepad"
        element_name = "Save"
        control_type = "Button"
        key_chord = ""
        is_password = False

    gate = DesktopTakeoverGate(db=db, approval_manager=ApprovalManager(db=db))
    d = await gate.check(_PartialAction(), session_id=_SESSION, mission_id=_MISSION_ID)

    assert d.allow is False
    assert d.reason == "malformed_action:text", (
        "a missing field must be REFUSED with a reason, not crash the loop"
    )
    assert d.request_id is None, "a refusal must not queue an approval row"
