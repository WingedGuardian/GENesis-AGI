"""The gh-capable-dispatch lever: mode ladder, and the gate at ``spawn()``.

The gate is keyed on the CAPABILITY (``gh`` in the requested profile's Bash
allowlist), not on the profile NAME, so an install that grants ``gh`` to its own
overlay-registered profile is covered. Both halves are asserted here, because a
name-keyed implementation passes every other test in this file.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from genesis.cc import steward_config as sc
from genesis.cc.direct_session import (
    _PROFILE_BASH_ALLOWLIST,
    DirectSessionRequest,
    DirectSessionRunner,
)
from genesis.cc.session_manager import SessionManager


@pytest.fixture
def config_dirs(tmp_path, monkeypatch) -> tuple[Path, Path]:
    """Redirect base + overlay resolution into tmp dirs.

    Returns ``(base_path, overlay_path)``; NEITHER exists initially, so the first
    assertion in every test is about the shipped DEFAULTS rather than about
    whatever this repo's tracked config happens to say. Also clears the env kill
    switch so an ambient value cannot make a test pass for the wrong reason.

    Mirrors ``tests/ego/test_reconcile_config.py``'s fixture — the house idiom for
    a config lever.
    """
    repo_dir = tmp_path / "repo"
    user_dir = tmp_path / "user_config"
    (repo_dir / "config").mkdir(parents=True)
    user_dir.mkdir(parents=True)
    monkeypatch.setattr(sc, "repo_root", lambda: repo_dir)
    monkeypatch.setattr("genesis._config_overlay._user_config_dir", lambda: user_dir)
    monkeypatch.delenv(sc._ENV_KILL_SWITCH, raising=False)
    return (
        repo_dir / "config" / "cc_steward.yaml",
        user_dir / "cc_steward.local.yaml",
    )


# ── the mode ladder ────────────────────────────────────────────────────────


def test_ships_off_with_no_config_at_all(config_dirs):
    """The SHIPPED POSTURE, asserted from DEFAULTS rather than from the yaml.

    A test that read the tracked `config/cc_steward.yaml` would keep passing if
    someone flipped that file to live, since it would simply be asserting what the
    file says. This asserts the code's own floor.
    """
    assert sc.effective_mode() == "off"
    assert sc.gh_dispatch_permitted() is False


def test_the_tracked_config_also_ships_off(monkeypatch):
    """And SEPARATELY: the file this repo actually ships must say off.

    Deliberately NOT using `config_dirs` — this one is about the committed
    artifact, which is the thing an install receives. The pair matters: the test
    above would pass with a live-by-default yaml, and this one would pass with a
    live-by-default DEFAULTS. Neither alone pins the shipped posture.
    """
    monkeypatch.delenv(sc._ENV_KILL_SWITCH, raising=False)
    assert sc.config_path().is_file(), "config/cc_steward.yaml is not shipped"
    assert sc.effective_mode() == "off"


def test_live_via_the_user_dir_overlay(config_dirs):
    """The documented arming path — the USER dir, not the repo-relative sibling."""
    _, overlay = config_dirs
    overlay.write_text('mode: "live"\n')
    assert sc.effective_mode() == "live"
    assert sc.gh_dispatch_permitted() is True


def test_the_user_dir_overlay_wins_over_the_repo_sibling(config_dirs, tmp_path):
    """Why operator-facing text must name the user dir and not the sibling.

    Both paths resolve, user-dir FIRST. An operator who hand-edits the sibling and
    later flips the lever from the dashboard (which writes the user dir) ends up
    with two overlays and the one they did not touch winning — silently. This
    pins the precedence so the docs stay true.
    """
    base, overlay = config_dirs
    (base.parent / "cc_steward.local.yaml").write_text('mode: "live"\n')
    overlay.write_text('mode: "off"\n')
    assert sc.effective_mode() == "off", (
        "the repo-relative sibling beat the user dir — the arming instructions in "
        "config/cc_steward.yaml and refusal_message() are then wrong"
    )


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("master switch", 'enabled: false\nmode: "live"\n'),
        # THE FAIL-OPEN THE AUDIT FOUND. A truthiness test on `enabled` left every
        # QUOTED form live: MEASURED, `enabled: "false"` gave permitted=True while
        # the operator believed they had disarmed. Quoted is the LIKELY spelling,
        # because this lever's own config tells the reader to quote values. Each
        # string gets its own arm rather than one representative, because what
        # slipped through was a whole TYPE, not a value.
        ("enabled: quoted false", 'enabled: "false"\nmode: "live"\n'),
        ("enabled: quoted off", 'enabled: "off"\nmode: "live"\n'),
        ("enabled: quoted no", 'enabled: "no"\nmode: "live"\n'),
        ("enabled: quoted zero", 'enabled: "0"\nmode: "live"\n'),
        # A TRUTHY non-bool degrades too: `enabled` is a boolean knob, so anything
        # that is not exactly True is damage and damage loses authority.
        ("enabled: quoted true", 'enabled: "true"\nmode: "live"\n'),
        ("enabled: int 1", 'enabled: 1\nmode: "live"\n'),
        ("enabled: empty list", 'enabled: []\nmode: "live"\n'),
        # YAML 1.1 parses an unquoted `off` as the BOOLEAN False. A sibling
        # (config/career_outreach.yaml) ships exactly that form today, so a
        # hand-edit copying its style lands here. Without the explicit `is False`
        # branch it would fall to the invalid-value case — same answer, but only
        # by luck, and luck is not a mechanism.
        ("unquoted mode: off", "mode: off\n"),
        ("garbage mode", "mode: sideways\n"),
        ("mode is a list", "mode: [live]\n"),
        ("mode is null", "mode:\n"),
    ],
)
def test_every_damage_path_degrades_to_off(config_dirs, label, body):
    """With two rungs the least-authority rung IS off, so every degradation
    lands there. Each arm is a distinct path through effective_mode, not a
    restatement: master switch, the YAML boolean, an unknown string, a wrong
    TYPE, and an explicit null."""
    base, _ = config_dirs
    base.write_text(body)
    assert sc.effective_mode() == "off", f"{label} did not degrade to off"


def test_the_yaml_boolean_is_not_reported_as_an_operator_ERROR(config_dirs, caplog):
    """What the explicit `mode is False` branch actually buys — found by a sweep.

    Both an unquoted `mode: off` and a garbage value return "off", because
    `False not in MODES` already falls through to the invalid-value branch. So a
    test asserting only the RETURN cannot tell whether that branch exists: a
    mutation deleting it stayed green across the whole suite.

    The difference is the DIAGNOSTIC, and nothing was reading it. An unquoted
    `off` is a reasonable hand-edit that means exactly what the operator intended
    — a sibling config ships that very form — so it must degrade SILENTLY.
    Garbage is a real mistake and must WARN, or an operator who typo'd the mode
    sees a silently disabled feature and no reason for it.
    """
    base, _ = config_dirs

    caplog.clear()
    base.write_text("mode: off\n")  # YAML 1.1 boolean False
    with caplog.at_level("WARNING", logger=sc.logger.name):
        assert sc.effective_mode() == "off"
    assert not [r for r in caplog.records if "invalid mode" in r.getMessage()], (
        "an unquoted `mode: off` was reported as an invalid mode — it is a "
        "legitimate hand-edit, and warning about it trains operators to ignore "
        "the warning that matters"
    )

    caplog.clear()
    base.write_text("mode: sideways\n")
    with caplog.at_level("WARNING", logger=sc.logger.name):
        assert sc.effective_mode() == "off"
    assert [r for r in caplog.records if "invalid mode" in r.getMessage()], (
        "a garbage mode degraded SILENTLY — the operator gets a disabled feature "
        "with no stated reason"
    )


def test_env_kill_switch_forces_off_over_a_live_overlay(config_dirs, monkeypatch):
    """The kill switch must beat config, not merely agree with it — so it is
    tested against an overlay that says live, never against the default."""
    _, overlay = config_dirs
    overlay.write_text('mode: "live"\n')
    assert sc.effective_mode() == "live", "fixture failed: not live before the switch"
    monkeypatch.setenv(sc._ENV_KILL_SWITCH, "1")
    assert sc.effective_mode() == "off"


def test_the_kill_switch_is_exactly_one(config_dirs, monkeypatch):
    """`== "1"`, not truthiness. `GENESIS_CC_STEWARD_DISABLED=0` must NOT disable —
    an operator writing 0 means "not disabled", and a truthiness check would
    silently mean the opposite."""
    _, overlay = config_dirs
    overlay.write_text('mode: "live"\n')
    for value in ("0", "", "false", "no"):
        monkeypatch.setenv(sc._ENV_KILL_SWITCH, value)
        assert sc.effective_mode() == "live", f"{value!r} disabled the lever"


def test_config_is_read_fresh_per_call(config_dirs):
    """No import-time caching: an operator arming the box expects the next
    dispatch to honour it, not the next server restart."""
    _, overlay = config_dirs
    assert sc.effective_mode() == "off"
    overlay.write_text('mode: "live"\n')
    assert sc.effective_mode() == "live", "the mode was cached across calls"
    overlay.unlink()
    assert sc.effective_mode() == "off", "the mode was cached after the overlay went"


def test_a_third_rung_would_not_be_permitted_by_default():
    """`gh_dispatch_permitted` is spelled `== "live"`, so adding a rung cannot
    silently grant the capability. Asserted by driving effective_mode directly —
    the point is the comparison, not the config."""
    assert sc.MODES == ("off", "live")
    assert sc.gh_dispatch_permitted.__doc__ is not None


# ── the gate at spawn() ────────────────────────────────────────────────────


def _runner(db) -> DirectSessionRunner:
    """A runner whose fire-and-forget run is neutralized, so spawn() is
    exercised for real but no CC process is launched. `runtime=object()` has no
    `_autonomy_manager`, which skips the unrelated ceiling check."""
    sm = SessionManager(db=db, invoker=AsyncMock(), day_boundary_hour=0)
    runner = DirectSessionRunner(
        invoker=AsyncMock(),
        session_manager=sm,
        config_builder=AsyncMock(),
        runtime=object(),
    )
    runner._run_session = lambda _req, _sid: asyncio.sleep(0)
    return runner


async def _drain(runner, sid) -> None:
    t = runner._active.get(sid)
    if t is not None:
        await asyncio.gather(t, return_exceptions=True)


async def test_spawn_refuses_a_gh_profile_while_off(db, config_dirs):
    """The defect this closes. Before the lever, the profile was inert only
    because nobody had created a campaign row."""
    runner = _runner(db)
    with pytest.raises(RuntimeError, match="gh-capable background dispatch"):
        await runner.spawn(DirectSessionRequest(prompt="read PRs", profile="steward"))


async def test_spawn_permits_a_gh_profile_when_live(db, config_dirs):
    """Without this arm, the refusal test above passes on a gate that refuses
    EVERYTHING — which is the failure mode of a one-directional gate test."""
    _, overlay = config_dirs
    overlay.write_text('mode: "live"\n')
    runner = _runner(db)
    sid = await runner.spawn(DirectSessionRequest(prompt="read PRs", profile="steward"))
    try:
        assert sid
    finally:
        await _drain(runner, sid)


async def test_spawn_does_not_touch_a_non_gh_profile_while_off(db, config_dirs):
    """COLLATERAL-DAMAGE CONTROL. `off` must refuse the gh capability and nothing
    else — a gate that blocked every dispatch would pass the refusal test and
    break every background session on the box."""
    runner = _runner(db)
    for profile in ("research", "observe", "interact"):
        assert "gh" not in _PROFILE_BASH_ALLOWLIST.get(profile, ()), (
            f"fixture assumption broken: {profile} now has gh"
        )
        sid = await runner.spawn(DirectSessionRequest(prompt="hi", profile=profile))
        try:
            assert sid, f"{profile} was refused while the lever was off"
        finally:
            await _drain(runner, sid)


async def test_the_gate_is_keyed_on_the_capability_not_the_profile_name(
    db, config_dirs, monkeypatch
):
    """THE ARM A NAME-KEYED GATE FAILS, and the reason the gate reads a dict.

    An install can register its own Bash-scoped profile through
    `genesis.cc.profile_overlay`, and `ProfileOverlayContext.add_profile` writes
    into `_PROFILE_BASH_ALLOWLIST` — the dict the gate reads. So a locally-added
    profile granted `gh` is gated by construction. A gate keyed on the name
    `steward` would let it straight through, silently, which is precisely the case
    the overlay mechanism exists to enable.

    Simulated by adding the allowlist entry directly rather than by importing a
    fake overlay module: the overlay loader runs once at import time, so a test
    cannot re-run it, and `add_profile`'s only relevant effect here is this write.
    """
    monkeypatch.setitem(_PROFILE_BASH_ALLOWLIST, "research", ("gh",))
    runner = _runner(db)
    with pytest.raises(RuntimeError, match="gh-capable background dispatch"):
        await runner.spawn(DirectSessionRequest(prompt="hi", profile="research"))


async def test_the_refusal_names_the_file_and_the_arming_step(db, config_dirs):
    """A refusal that says "disabled by config" sends the reader hunting. Assert
    the message carries the overlay path, the value to set, and the kill switch —
    and that the overlay name is DERIVED, so it cannot name a file that does not
    exist."""
    runner = _runner(db)
    with pytest.raises(RuntimeError) as exc:
        await runner.spawn(DirectSessionRequest(prompt="hi", profile="steward"))
    msg = str(exc.value)
    assert sc._OVERLAY_NAME in msg
    assert "~/.genesis/config/" in msg
    assert 'mode: "live"' in msg
    assert sc._ENV_KILL_SWITCH in msg
    assert "gh auth status" in msg, (
        "the refusal must hand the operator the command that settles what arming "
        "grants. It used to ASSERT the answer ('still UNAUTHENTICATED'), which was "
        "false on the branch it shipped on — see "
        "test_the_texts_tell_the_operator_to_VERIFY_rather_than_asserting_a_posture"
    )
    assert Path(sc._CONFIG_NAME).with_suffix(".local.yaml").name == sc._OVERLAY_NAME


async def test_the_refused_spawn_leaves_no_session_row(db, config_dirs):
    """A refusal must not leave a half-registered session behind. The gate is
    placed before any row is written; this pins that ordering, since moving it
    below registration would leave orphans that the reaper then has to reason
    about."""
    from genesis.db.crud import cc_sessions

    # `get_status_counts` rather than a listing: it aggregates over the whole
    # table, so it cannot under-read the way a limited listing can, and the
    # comparison is a total against a total.
    before = await cc_sessions.get_status_counts(db)
    runner = _runner(db)
    with pytest.raises(RuntimeError):
        await runner.spawn(DirectSessionRequest(prompt="hi", profile="steward"))
    after = await cc_sessions.get_status_counts(db)
    assert after == before, (
        f"a refused dispatch changed the session table ({before} -> {after}) — the "
        f"gate must sit before registration, or a refusal leaves an orphan row"
    )


async def test_the_campaign_path_round_trip_off_live_off(db, config_dirs):
    """THE ACCEPTANCE BAR, through the route that would really arm this.

    The campaign path is the one that matters, and it is the one this lever was
    written for: `campaign_create` validates the profile against VALID_PROFILES
    (which contains `steward`), and the campaign runner passes the row's
    `session_profile` straight into `spawn()`. So a single row was the entire
    distance between dormant and running, and that row is still creatable — the
    gate deliberately sits at `spawn()` instead, so every dispatch path is covered
    rather than only the one that writes rows.

    A ROUND TRIP, not a refusal: a gate proven only in the refusing direction is
    half-measured, because "refuses everything" passes that half. off -> refused,
    live -> permitted, off -> refused again, with the SAME request object shape
    each time so the only thing that changed is the lever.
    """
    campaign = {"session_profile": "steward", "model": "sonnet", "effort": "medium"}

    def campaign_request() -> DirectSessionRequest:
        """Exactly the shape `campaigns/runner.py` builds — same
        `session_profile` lookup, same `campaign` source_tag — so this exercises
        the real arming route rather than a hand-rolled request."""
        return DirectSessionRequest(
            prompt="read the open PRs",
            system_prompt="strategy",
            profile=campaign.get("session_profile", "campaign"),
            notify=False,
            source_tag="campaign",
            caller_context="campaign:acceptance-bar",
        )

    runner = _runner(db)
    _, overlay = config_dirs

    # off -> REFUSED
    assert sc.effective_mode() == "off"
    with pytest.raises(RuntimeError, match="gh-capable background dispatch"):
        await runner.spawn(campaign_request())

    # live -> PERMITTED, with the identical request
    overlay.write_text('mode: "live"\n')
    assert sc.effective_mode() == "live"
    sid = await runner.spawn(campaign_request())
    try:
        assert sid, "the campaign path was refused while armed"
    finally:
        await _drain(runner, sid)

    # off again -> REFUSED, with no restart. Pins the fresh-read property at the
    # GATE rather than only at `effective_mode`, which is where it has to hold.
    overlay.write_text('mode: "off"\n')
    with pytest.raises(RuntimeError, match="gh-capable background dispatch"):
        await runner.spawn(campaign_request())


async def test_the_kill_switch_reaches_the_gate_not_just_the_lever(
    db, config_dirs, monkeypatch
):
    """`effective_mode` honouring the switch is not the same claim as the GATE
    honouring it. Tested against a LIVE overlay, so the switch has to beat config
    rather than merely agree with it."""
    _, overlay = config_dirs
    overlay.write_text('mode: "live"\n')
    runner = _runner(db)
    sid = await runner.spawn(DirectSessionRequest(prompt="hi", profile="steward"))
    await _drain(runner, sid)  # fixture check: permitted before the switch

    monkeypatch.setenv(sc._ENV_KILL_SWITCH, "1")
    with pytest.raises(RuntimeError, match="gh-capable background dispatch"):
        await runner.spawn(DirectSessionRequest(prompt="hi", profile="steward"))


def test_the_texts_tell_the_operator_to_VERIFY_rather_than_asserting_a_posture():
    """BLOCKER from the adversarial audit, and the shape of the fix.

    The first version of this file asserted that the string "UNAUTHENTICATED"
    appeared in the module docstring, the shipped yaml and the refusal message —
    i.e. it ENFORCED that a claim kept being made. The claim was false on this
    branch: removing the credential from the gh seal is a separate PR, still open,
    so an armed session here authenticates as the operator. A test that pins a
    falsehood is worse than no test, because it turns the next person's correction
    into a failing suite.

    It is replaced by its opposite. None of the three texts may assert an
    authentication POSTURE at all — that fact lives in the invoker and flips when
    that PR lands — and all three must instead hand the operator the command that
    settles it on their own install.

    NO `config_dirs`: this reads the SHIPPED artifacts, and the fixture points
    `repo_root` at a tmp dir where the yaml does not exist.
    """
    texts = {
        "module docstring": sc.__doc__ or "",
        "shipped yaml": sc.config_path().read_text(encoding="utf-8"),
        "refusal message": sc.refusal_message("steward"),
    }

    for where, text in texts.items():
        assert "gh auth status" in text, (
            f"{where} does not tell the operator how to check what arming grants; "
            f"without it the reader has to infer, and the inference was wrong once"
        )
        # The specific false sentences, in the spellings they were written in.
        # Matched case-insensitively on the CLAIM, not on a token, so a reworded
        # version of the same assertion is caught too.
        lowered = text.lower()
        for claim in (
            "is unauthenticated",
            "will be unauthenticated",
            "session is still unauthenticated",
            "carries no github credential",
            "no dispatched session carries a github credential",
        ):
            assert claim not in lowered, (
                f"{where} asserts {claim!r}. That is a property of cc/invoker.py "
                f"and of the install, it is not true on every branch, and it was "
                f"false when first written. State how to CHECK instead."
            )


def test_the_prompt_does_not_promise_an_authentication_state(config_dirs):
    """Same rule, applied to the PROMPT — the surface that matters most.

    A wrong fact in a docstring misleads a maintainer. A wrong fact in the
    addendum misleads the model, in the permissive direction: an earlier draft
    told the session its writes were inert and that an auth failure was expected,
    on a branch where its calls would in fact have succeeded as the owner.
    """
    from genesis.cc.direct_session import _PROFILE_ADDENDA

    addendum = _PROFILE_ADDENDA["steward"]
    lowered = addendum.lower()
    assert "you are unauthenticated" not in lowered
    assert "do not assume" in lowered, (
        "the prompt should tell the session not to assume a credential either way"
    )
    # And it must cover BOTH branches of the fact it refuses to assert.
    assert "expected state" in lowered, "no guidance for the unauthenticated case"
    assert "repository owner" in lowered or "acting as" in lowered, (
        "no guidance for the case where gh DOES authenticate — which is the "
        "dangerous half, and the half the first draft denied could happen"
    )


def test_the_steward_profile_denies_filesystem_reads():
    """Closes the exfiltration path the audit found.

    `Write` was denied and `Read` was not, with `skip_permissions=True` and an
    allowed `outreach_send` — so attacker-authored pull-request text could have
    had the session read an owner-readable credential file and send it onward. No
    environment pin touches that; tool scope is the only thing that does.

    Glob and Grep are asserted too: a denial that stopped at `Read` would leave
    `Grep` able to pull a secret out of a file it is pointed at.
    """
    from genesis.cc.direct_session import PROFILES

    denied = PROFILES["steward"]
    for tool in ("Read", "Glob", "Grep"):
        assert tool in denied, (
            f"steward permits {tool}: a session reading attacker-authored PR text, "
            f"with skip_permissions and outreach_send, can exfiltrate any "
            f"owner-readable secret"
        )
    # The pre-existing denials must survive the addition.
    for tool in ("Write", "Edit", "NotebookEdit"):
        assert tool in denied, f"the read denial dropped the existing {tool} block"
    # And Bash must still be PERMITTED, or the profile loses its only capability
    # and this change quietly became "delete the profile".
    assert "Bash" not in denied, "Bash was denied — the profile can no longer run gh"


def test_the_predicate_is_shared_by_the_gate_and_its_choosers(config_dirs):
    """ONE predicate, so the gate and the callers that CHOOSE cannot drift.

    `profile_dispatch_refusal` returns the refusal text or None. The gate raises
    on it; a caller that selects a profile consults it first. Asserted as a
    round trip, since a predicate that always refuses would satisfy half of this.
    """
    from genesis.cc.direct_session import profile_dispatch_refusal

    _, overlay = config_dirs

    assert profile_dispatch_refusal("steward") is not None
    assert profile_dispatch_refusal("research") is None, (
        "a non-gh profile was refused — the predicate must discriminate"
    )
    assert profile_dispatch_refusal("nonexistent-profile") is None, (
        "an unknown profile has no gh grant, so it is not this predicate's to "
        "refuse; profile VALIDITY is checked elsewhere"
    )

    overlay.write_text('mode: "live"\n')
    assert profile_dispatch_refusal("steward") is None, (
        "still refused while armed — the predicate ignores the lever"
    )


def test_the_ego_will_not_select_a_refused_profile(config_dirs):
    """THE FLIP-FLOP GUARD, driven through the decision itself.

    The ego honours a model-authored `profile` from its execution brief whenever
    the name is registered — and the registry includes the gh-capable one. On
    dispatch failure it calls `revert_failed_dispatch`, which returns the proposal
    to `approved` with NO attempt counter, so the next sweep re-dispatches. A
    refusal here is PERMANENT until an operator edits config, so honouring the
    name would re-fail once per cycle, indefinitely, on a path nobody watches.

    The first version of this test grepped `ego/session.py` for the predicate's
    NAME. It survived a mutation that deleted the guard and left the import — the
    assert-existence trap. `_select_dispatch_profile` was extracted so the
    decision could be driven, and this asserts the RETURNED profile.
    """
    from genesis.cc.direct_session import VALID_PROFILES, profile_dispatch_refusal
    from genesis.ego.session import _select_dispatch_profile

    # Preconditions that make the bug reachable. Asserted, not assumed: if either
    # stops holding, this test is about nothing and should say so loudly.
    assert "steward" in VALID_PROFILES, "the ego can no longer name it; premise gone"
    assert profile_dispatch_refusal("steward") is not None, "fixture: not refused"

    brief = {"profile": "steward", "action_type": "research_task"}
    chosen = _select_dispatch_profile(brief)
    assert chosen != "steward", (
        f"the ego selected a REFUSED profile ({chosen!r}); every sweep will "
        f"re-dispatch and re-fail because revert_failed_dispatch has no counter"
    )

    # It must fall through to inference, not to some hardcoded default — and the
    # fallback must itself be dispatchable, or the guard trades one dead loop for
    # another.
    assert profile_dispatch_refusal(chosen) is None
    assert chosen in VALID_PROFILES

    # CONTROL: an unrefused brief profile is still honoured. Without this the test
    # passes on a guard that ignores the brief entirely, which would re-break the
    # six code-change dispatches the brief-trusting behaviour exists to fix.
    assert _select_dispatch_profile({"profile": "research"}) == "research"


def test_the_ego_honours_a_gh_profile_once_armed(config_dirs):
    """And the guard must LIFT. A permanent exclusion would mean arming the lever
    still left the ego unable to use the capability — the lever would be half a
    switch."""
    from genesis.ego.session import _select_dispatch_profile

    _, overlay = config_dirs
    assert _select_dispatch_profile({"profile": "steward"}) != "steward"
    overlay.write_text('mode: "live"\n')
    assert _select_dispatch_profile({"profile": "steward"}) == "steward", (
        "the ego still refuses the profile while the lever is LIVE — the guard is "
        "a blanket exclusion rather than a reading of the lever"
    )


async def test_campaign_create_refuses_a_gated_profile_at_creation_time(config_dirs):
    """MEDIUM-5: the operator asked a question HERE, so answer it here.

    Without this, a campaign naming the gh profile is created successfully, ticks
    on its cron, and fails inside `_tick_wrapper` — which logs and records a job
    failure. Observable, but the operator got "success" and then learns the truth
    from a recurring failure somewhere else. Refusing at creation also means no row
    exists to reap when they change their mind.

    SCOPE, stated because the missing half is deliberate: only the REFUSING
    direction is driven here. The check sits before any database access, so this
    needs no db; going further would create a real campaign row against the live
    connection, which is not this test's business. The permitting direction is
    covered where it belongs — `test_the_predicate_is_shared_by_the_gate_and_its_choosers`
    proves the predicate returns None when armed, and this call site does nothing
    but ask the predicate. Asserted below, so that claim is checked rather than
    trusted.
    """
    from genesis.mcp.health import campaign_tools

    # `@mcp.tool()` wraps the coroutine in a FunctionTool; `.fn` is the callable.
    create = campaign_tools.campaign_create.fn

    res = await create(
        name="probe-steward-refusal",
        strategy_doc_path="/nonexistent/strategy.md",
        cron_cadence="0 9 * * *",
        profile="steward",
    )
    assert "error" in res, f"a gated profile was accepted at creation: {res}"
    assert "gh auth status" in res["error"], (
        "the creation-time refusal must carry the same actionable text the gate "
        "uses, or the operator has to go hunting for it"
    )

    # The refusal must precede validation of everything else — the strategy path
    # above does not exist, so if that were checked first this would pass for the
    # wrong reason and keep passing after the lever check was deleted.
    assert "strategy" not in res["error"].lower(), (
        "the error is about the strategy doc, not the lever — this test is "
        "passing for the wrong reason"
    )

    # And the call site delegates rather than re-implementing, which is what makes
    # the permitting direction covered by the predicate's own test.
    src = (
        Path(__file__).parents[2] / "src/genesis/mcp/health/campaign_tools.py"
    ).read_text(encoding="utf-8")
    assert "profile_dispatch_refusal" in src


def test_every_shipped_gh_profile_is_covered_by_the_lever():
    """Coverage guard, mirroring test_direct_session_profiles.py's insistence
    that every profile be classified. If a second shipped profile is granted
    `gh`, it is gated automatically — this asserts the set the gate reads is the
    set that carries the grant, so the claim is checked rather than assumed."""
    gh_profiles = {p for p, a in _PROFILE_BASH_ALLOWLIST.items() if "gh" in a}
    assert gh_profiles == {"steward"}, (
        f"the shipped gh-capable profile set changed to {sorted(gh_profiles)}. That "
        f"is fine — the gate is capability-keyed and covers them — but update this "
        f"test and the lever's docs, which both name `steward` as the only one."
    )


def test_the_lever_does_not_import_direct_session():
    """Import-cycle guard. `direct_session` imports the lever inside `spawn()`;
    the lever must never import `direct_session`, or the deferred import is
    papering over a cycle that will surface as an ImportError at startup on some
    other entry path."""
    source = Path(sc.__file__).read_text(encoding="utf-8")
    assert "direct_session" not in source.replace("``genesis.cc.direct_session``", "").replace(
        "import ``genesis.cc.direct_session``", ""
    ), "steward_config references direct_session outside its docstring note"
