"""Tests for /api/genesis/zero-drop — the accounting view's dashboard surface.

Split per the calibration/cc-sessions route-test precedent: the route WIRING
(registration, the not-bootstrapped guard, the limit parameter) is pinned
synchronously with a mocked runtime, because ``_async_route`` owns its own
event loop and cannot run inside an async test's loop. The view's CONTENT is
tested against a real DB in tests/test_session_awareness/test_zero_drop_view.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

from genesis.dashboard.api import blueprint


@pytest.fixture()
def client():
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    app.config["TESTING"] = True
    return app.test_client()


def _rt(db=None, *, bootstrapped=True):
    rt = MagicMock()
    rt.is_bootstrapped = bootstrapped
    rt.db = db
    return rt


def test_the_route_is_registered_on_the_blueprint():
    """Level-3 wiring: the endpoint exists on the app, not merely in a module.

    A route module that is never imported registers nothing, and the import
    happens by side effect through routes/__init__.py — so a module added to
    the package but missing from that list is silently absent.
    """
    app = Flask(__name__)
    app.register_blueprint(blueprint)
    rules = {str(r) for r in app.url_map.iter_rules()}
    assert "/api/genesis/zero-drop" in rules


def test_a_not_bootstrapped_runtime_reports_UNAVAILABLE_not_an_empty_board(client):
    """The distinction this whole surface exists to preserve.

    Returning empty parts here would render as a clean board — zero stranded,
    zero pending — which is exactly the false-clean the detector was built to
    prevent. The runtime being down is unknown, never zero.
    """
    with patch("genesis.runtime.GenesisRuntime.instance", return_value=_rt(bootstrapped=False)):
        resp = client.get("/api/genesis/zero-drop")

    assert resp.status_code == 200
    body = resp.get_json()
    assert body["status"] == "unavailable"
    assert "not zero" in body["reason"], "the reason must say WHY an empty answer is wrong"
    assert "gaps" not in body, "no part may be rendered as a count when nothing was read"


def test_the_view_is_returned_whole(client):
    """The route does no assembly of its own — it returns what build_view says.

    Pinned because a route that reshapes the view is how two surfaces start
    disagreeing about the same board.
    """
    sentinel = {"computed_at": "2026-09-07T00:00:00+00:00", "gaps": {"status": "ok", "open": 3}}

    async def _fake(db, *, now, findings_limit):
        return sentinel

    with (
        patch("genesis.runtime.GenesisRuntime.instance", return_value=_rt(db=object())),
        patch("genesis.session_awareness.zero_drop_view.build_view", _fake),
    ):
        resp = client.get("/api/genesis/zero-drop")

    assert resp.get_json() == sentinel


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("", 20),  # the default page size
        ("?limit=5", 5),
        ("?limit=99999", 200),  # clamped to _MAX_LIMIT
        ("?limit=0", 1),  # clamped up: a zero-row page is not a listing
        ("?limit=notanumber", 20),  # unparseable falls back, never raises
    ],
)
def test_the_limit_parameter_is_clamped_rather_than_trusted(client, query, expected):
    """The page size is bounded; the DENOMINATOR beside it is not.

    Clamping is safe here only because this bounds a page rather than a total —
    the counts come from full COUNTs, so a clamped page still renders "n of N".
    """
    seen = {}

    async def _fake(db, *, now, findings_limit):
        seen["limit"] = findings_limit
        return {"computed_at": "x"}

    with (
        patch("genesis.runtime.GenesisRuntime.instance", return_value=_rt(db=object())),
        patch("genesis.session_awareness.zero_drop_view.build_view", _fake),
    ):
        client.get(f"/api/genesis/zero-drop{query}")

    assert seen["limit"] == expected


def test_the_dashboard_renders_the_SAFE_identity_not_the_ack_key():
    """The API splits the identity into three fields so this cannot be got
    wrong, and the first consumer got it wrong anyway.

    `zero_drop_status` emits `branch` (the VERBATIM ack key, which must
    round-trip unsanitised), `branch_display` (neutralised, safe to put in
    front of a person) and `identity_unrenderable` (the two differ).
    `git check-ref-format` accepts bidi overrides and zero-width characters, so
    a ref name can RENDER as something other than the key an operator is
    acknowledging. `x-text` is not the defence — it stops HTML injection, and
    this is not an injection problem.

    This is a source-level assertion because there is no browser in the suite;
    it is worth having anyway, since it fails the moment someone reverts to the
    shorter field name, which is the whole failure mode.
    """
    import pathlib

    tpl = (
        pathlib.Path(__file__).resolve().parents[2]
        / "src/genesis/dashboard/templates/partials/tabs/zero_drop.html"
    ).read_text()

    # This line has been wrong in BOTH directions, so both are pinned.
    #
    # `|| f.branch` leaked the verbatim ack key, because a branch made entirely
    # of bidi or zero-width characters neutralises to `''`, which is falsy.
    # The correction to that overshot: `?? '[unrenderable]'` stopped the leak
    # but `??` falls back only on null/undefined, so the same branch rendered
    # as a BLANK identity — no key, no placeholder, nothing.
    #
    # `|| '[unrenderable]'` is the form that handles both: falsy is exactly the
    # condition, and `''` is exactly the value that matters. The operator was
    # never the bug; the fallback TARGET was.
    assert "f.branch_display || '[unrenderable]'" in tpl, (
        "an empty sanitised name must render the PLACEHOLDER — not the raw ack "
        "key (`|| f.branch`) and not nothing at all (`?? ...`)"
    )
    assert "f.branch_display || f.branch" not in tpl, (
        "falling back to the raw ack key is the original defect"
    )
    assert "f.branch_display ??" not in tpl, (
        "`??` does not catch the empty string, which is the ONLY case this fallback exists for"
    )
    assert "f.identity_unrenderable" in tpl, (
        "and must TELL the reader when what they see is not the ack key"
    )
    # The bare field in a text position is the regression to catch. It stays
    # legal as an x-for :key, which is never rendered.
    assert 'x-text="f.branch"' not in tpl, (
        "rendering the verbatim ack key is the defect this test exists for"
    )


def test_the_finding_row_guards_a_NULL_ahead_count_and_shows_the_worktree():
    """Two renders that were unconditional over a nullable field.

    `classify_worktrees` creates `dirty_worktree` findings with NO
    `ahead_count` — by design, and the column is nullable — so an unguarded
    `' +' + f.ahead_count` printed the literal `+null` on every row of the
    third supported finding class.

    The same row rendered only the identity, but a dirty-worktree identity is
    `branch:<digest>` when one branch is checked out in several worktrees and
    `@opaque:<digest>` when the natural identity is unsafe. In both cases the
    operator cannot tell WHICH worktree holds the edits — which is precisely
    why the API supplies a neutralised `worktree_path` beside it.
    """
    import pathlib

    tpl = (
        pathlib.Path(__file__).resolve().parents[2]
        / "src/genesis/dashboard/templates/partials/tabs/zero_drop.html"
    ).read_text()

    assert "Number.isFinite(f.ahead_count)" in tpl, (
        "the ahead-count suffix must render only when the value is numeric — "
        "every dirty_worktree finding has none"
    )
    # Assert the GUARD, not just the field name. The first version of this
    # checked `"f.worktree_path" in tpl`, which stays true when the element is
    # hidden — the field name still appears in the `x-text`. That test passed
    # against a permanently invisible row, which is the thing it exists to
    # catch. The condition is what makes it render.
    assert 'x-show="f.worktree_path"' in tpl, (
        "a dirty finding whose identity is a digest needs its readable path "
        "SHOWN — gating it on anything else hides the only way to tell which "
        "worktree holds the edits"
    )
    assert "' @ ' + f.worktree_path" in tpl, "and the path itself must be rendered"


def test_the_header_badge_carries_its_denominator_and_refuses_a_stale_count():
    """The first thing read, and the one count with no qualifier.

    A bare `N stranded` rendered identically whether the detector had just
    swept cleanly or was stale and blind — presenting leftover findings as
    current, above the qualified body. That is this panel's own accounting
    invariant broken in its most prominent position.

    The badge does not go blank in that state: it says WHICH fault it is
    ("detector stale" / "detector blind") and that what follows is not a
    measurement. Replacing a misleading number with the reason it is
    misleading is the whole point — the same thing the PR cache does when it
    reports `stale` and a verdict instead of a number it cannot stand behind.
    """
    import pathlib

    tpl = (
        pathlib.Path(__file__).resolve().parents[2]
        / "src/genesis/dashboard/templates/partials/tabs/zero_drop.html"
    ).read_text()

    header = tpl.split("panel-body")[0]
    assert "gaps.listed_of" in header, "the badge must carry its denominator"
    assert "detector?.stale" in header and "detector?.blind" in header, (
        "a stale or blind detector must change what the badge SAYS, not print "
        "a number it cannot stand behind"
    )
    assert "not a count" in header and "detector" in header, (
        "and must name the fault — the reader is told why, never left with a "
        "blank where a figure used to be"
    )


def test_the_board_renders_the_deferred_lane_and_a_superseded_fetch_is_dropped():
    """Two renders whose absence recreated the defects they were fixed for.

    The backend gained a `deferred` count so a store holding only tabled rows
    would stop reporting `0 of 0` — and the renderer was never taught to show
    it, so the false zero stayed on the screen. Fixing the data and leaving the
    surface is not fixing the false zero.

    Separately, every fetch response overwrote the panel with no check for a
    newer in-flight request. A response outliving the 60s interval, or one
    still in flight across a tab exit and re-entry, could land last and install
    an OLDER board — then stamp `lastSuccess`, so the stale data read as
    transport-healthy and the stale banner never fired.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2]
    tpl = (root / "src/genesis/dashboard/templates/partials/tabs/zero_drop.html").read_text()
    js = (root / "src/genesis/dashboard/webui/js/dashboard.js").read_text()

    # Assert the GUARD, not the identifier. A bare `"part.deferred" in tpl`
    # stays true when the element is hidden — the name still sits in the
    # `x-text` — so it passes against a row nobody can see, which is precisely
    # what it exists to catch. (This test was written that way first, in the
    # same session as the note saying not to.)
    assert 'x-show="part.deferred"' in tpl, (
        "the deferred lane must reach the SCREEN — counting it in the backend "
        "alone leaves `0 of 0` in front of the reader"
    )
    assert "' (+' + part.deferred + ' deferred)'" in tpl, "and the count itself must render"
    # BOTH halves, for both fields. A guard with a blanked body renders
    # nothing; a body with no guard renders `undefined`. Checking one and not
    # the other leaves the mutation that removes the other alive — which is
    # exactly what happened here on the first two attempts at this test.
    assert 'x-show="$store.genesisDashboard.zeroDropView.pr_pipeline?.repo"' in tpl, (
        "the repository scope needs its guard"
    )
    assert "'repo: ' + $store.genesisDashboard.zeroDropView.pr_pipeline.repo" in tpl, (
        "...and the scope itself must actually be rendered — a count with no "
        "repository reads as correct for the wrong one"
    )

    assert "_zeroDropFetchToken" in js, "fetches need a monotonic token"
    assert js.count("token !== this._zeroDropFetchToken") >= 2, (
        "the token must be re-checked AFTER the json() await too — parsing the "
        "body is a second suspension point where a newer response can land"
    )
