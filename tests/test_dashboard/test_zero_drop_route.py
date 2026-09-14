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
    tpl = _zero_drop_template()

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
    tpl = _zero_drop_template()

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
    tpl = _zero_drop_template()

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
    tpl = _zero_drop_template()
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
    # The deferred lane renders its OWN numerator AND denominator. A bare
    # `+N deferred` beside the actionable total put two populations behind one
    # denominator — the board read "212 of 348 (+322 deferred)" on live data,
    # where 348 is not what 322 is out of.
    assert "part.deferred_open" in tpl, (
        "the deferred lane needs its own numerator on screen, not just a total"
    )
    assert "' open of '" in tpl and "' deferred)'" in tpl, (
        "...and its own denominator beside it, so neither figure borrows the actionable lane's"
    )
    assert "' (+' + part.deferred + ' deferred)'" not in tpl, (
        "the bare scalar is the mixed-denominator defect"
    )
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


def _strip_comments(markup: str) -> str:
    """Remove HTML comments before asserting anything about markup.

    Every source-level assertion in this file is a substring check, and this
    template carries long explanatory comments that quote the very expressions
    being asserted. An adversarial audit showed the consequence: wrapping a whole
    `<span x-text="…">` in `<!-- -->` deletes the behaviour and leaves every test
    green, because the strings are all still in the file. Commented-out code is
    not code.
    """
    import re

    return re.sub(r"<!--.*?-->", "", markup, flags=re.S)


def _zero_drop_template() -> str:
    import pathlib

    return _strip_comments(
        (
            pathlib.Path(__file__).resolve().parents[2]
            / "src/genesis/dashboard/templates/partials/tabs/zero_drop.html"
        ).read_text()
    )


def _header_badges(tpl: str) -> list[tuple[str, str]]:
    """The two `panel-status` chips as (x-show condition, rest of the chip).

    Three separations, each learned by a mutation surviving.

    Per CHIP, because asserting against the whole header would pass if a clause
    landed on the WRONG badge — gating the warning on transport-stale instead of
    the number is the same strings in the same file and exactly inverted
    behaviour.

    Per ATTRIBUTE, because the warning chip NAMES its conditions twice: once in
    `x-show` to decide whether it renders, and once in `x-text` to say which
    faults hold. A substring check against the whole chip is satisfied by the
    text occurrence, so deleting the condition from `x-show` — which makes the
    chip vanish in exactly the state it exists for — left the test green.

    And without comments, per `_strip_comments` above.

    What none of that gives is EVALUATION. These remain string assertions about
    a JavaScript expression, so the tests below also pin the boolean SHAPE of
    each condition (operator counts) rather than only the presence of a clause:
    an audit flipped `&&` to `||` and wrapped a clause in `|| true`, each
    inverting the behaviour while leaving every asserted substring in place.
    """
    header = tpl.split("panel-body")[0]
    chips = header.split('<span class="panel-status"')[1:]
    assert len(chips) == 2, f"expected the number chip and the warning chip, got {len(chips)}"
    out = []
    for chip in chips:
        assert chip.count(' x-show="') == 1, "a chip has no single x-show to isolate"
        condition, _, rest = chip.split(' x-show="', 1)[1].partition('">')
        assert condition and rest, "x-show did not terminate — the attribute parse is wrong"
        # The BOUND VALUE, not the rest of the chip. Extracting it is itself an
        # assertion: rename the attribute and this raises rather than passing.
        # A mutation that renamed `x-text` to `data-x-text` — which disables the
        # binding completely, so the chip renders an empty span — left every
        # downstream assertion green, because all of them were substring checks
        # over markup in which the expression was still present as dead text.
        # Both attributes are matched with a LEADING SPACE for the same reason:
        # `"x-text=" in chip` is true of `data-x-text=`.
        assert ' x-text="' in rest, "the chip must BIND its text, not merely contain one"
        text, _, tail = rest.split(' x-text="', 1)[1].partition('">')
        assert text and "</span>" in tail, "x-text did not terminate — the parse is wrong"
        out.append((condition, text))
    return out


def test_the_badge_withholds_its_number_when_the_REFRESH_is_failing_too():
    """The axis the badge's own rule was never applied to.

    The panel already refuses to print a number when the DETECTOR is stale or
    blind. It kept printing one when the TRANSPORT was — a refresh failing after
    a board had loaded leaves the body carrying a loud "Refresh is FAILING …"
    banner while the chip above it shows a confident figure for a board that may
    be hours old. Found by rendering the panel in a browser; no test reached it,
    because every test read the payload rather than the page.

    `loading` and `error` are deliberately NOT in the condition: both mean no
    fetch has ever succeeded, so there is no `lastSuccess`, no `zeroDropView`,
    and no chip to mislabel.
    """
    (number_show, _), (warning_show, warning_body) = _header_badges(_zero_drop_template())

    assert "!$store.genesisDashboard.refreshFailing('zeroDrop')" in number_show, (
        "the NUMBER must be withheld while the refresh is failing — the figure "
        "is from the last board that loaded, not from now"
    )
    # In the x-show, not merely somewhere in the chip: if the warning renders
    # only on detector faults, a transport-stale board with a healthy detector
    # shows no chip at all — a blank where the qualifier belongs, which is the
    # silent version of the same defect.
    assert "refreshFailing('zeroDrop')" in warning_show, (
        "and the WARNING must be what appears instead"
    )
    # The PHASE is not the fault. `startFetch` sets `refreshing` for the whole
    # duration of every attempt, so gating on `panelState(…) === 'stale'` brings
    # the number back during each retry — and under a sustained 5xx outage the
    # phase never reaches `stale` at all, because the backoff outlives the poll
    # interval and every superseded response returns before `failFetch`.
    assert "panelState('zeroDrop') === 'stale'" not in warning_show, (
        "gating on the phase is the defect: it withholds only between attempts"
    )
    assert "panelState('zeroDrop') !== 'stale'" not in number_show, (
        "same, on the chip that actually carries the number"
    )
    assert "refresh failing" in warning_body, (
        "naming the fault is the point: a reader who sees a warning with no "
        "cause cannot tell a broken detector from a broken fetch"
    )

    # SHAPE, not just presence. An audit inverted both chips without removing a
    # single asserted substring: `&&` flipped to `||` on the number chip makes it
    # show a figure even when the detector is blind AND stale, and `|| true`
    # appended to the same clause makes it show one always. Operator counts are
    # blunt and will trip on a legitimate refactor — which is the correct
    # failure mode for a guard whose subject is a boolean nobody can execute here.
    assert (number_show.count("&&"), number_show.count("||")) == (4, 0), (
        "the number chip is a conjunction of five conditions — status ok, open "
        "> 0, detector not stale, detector not blind, transport not stale. Any "
        "disjunction in it means one fault can no longer withhold the number."
    )
    assert (warning_show.count("&&"), warning_show.count("||")) == (1, 2), (
        "the warning chip is `status ok AND (any of three faults)` — exactly one "
        "conjunction and two disjunctions"
    )


def test_the_badge_warning_JOINS_its_causes_instead_of_ranking_them():
    """A ternary here printed one fault and swallowed the others.

    `blind ? 'detector blind' : 'detector stale'` renders "detector blind" for a
    detector that is blind AND stale — so a reader who fixes the blind leg sees
    the number return with the staleness still true. The badge is an inventory
    of what is wrong, not a verdict about which wrongness outranks which.
    """
    _, (_, warning_chip) = _header_badges(_zero_drop_template())

    for cause in ("detector blind", "detector stale", "refresh failing"):
        assert f"'{cause}'" in warning_chip, f"`{cause}` must be one of the listed causes"

    assert ".filter(Boolean).join(" in warning_chip, (
        "the causes must be JOINED — the moment one is chosen over another, a "
        "second simultaneous fault stops being visible"
    )
    # The ranked form baked the suffix into each branch:
    #   blind ? 'detector blind — not a count' : 'detector stale — not a count'
    # so the phrase appeared once PER CAUSE. Appending it once to a joined list
    # is the shape that cannot rank, and the count is what distinguishes them —
    # a substring check for the old ternary would pass against any reworded
    # version of the same mistake.
    assert warning_chip.count("— not a count") == 1, (
        "the qualifier is appended ONCE to the joined causes; one copy per "
        "branch means the branches are exclusive, which is the ranking defect"
    )

    # Each cause must be tested by ITSELF. Joining the list is not enough on its
    # own: an audit restored the ranking inside the list by rewriting one entry
    # as `(!blind && stale) ? 'detector stale' : null`, which reads as a joined
    # list and behaves as a precedence order. A cause whose test mentions another
    # cause is a ranking wearing a list's clothes.
    import re as _re

    for cause in ("detector blind", "detector stale", "refresh failing"):
        m = _re.search(r"([^,\[\n]*?)\?\s*'" + _re.escape(cause) + r"'\s*:\s*null", warning_chip)
        assert m, f"`{cause}` must be a `<test> ? '{cause}' : null` entry in the list"
        test = m.group(1)
        assert not any(op in test for op in ("&&", "||", "!")), (
            f"the test for `{cause}` carries a boolean operator ({test.strip()!r}) — "
            "it must depend on its OWN fault and nothing else, or the list ranks"
        )


def test_an_unreadable_PR_cache_says_UNAVAILABLE_in_words_not_just_in_amber():
    """Three parts said it; the fourth left it to the colour.

    `pr_pipeline` has three statuses. `ok` and `stale` both carry a `verdict`
    written to stand alone ("cache is 27h old (TTL 24h) — the pulse worker is not
    running"). `unavailable` carries only a `reason`, and the line rendered that
    reason BARE — so the part that could not be READ looked like a part reporting
    a fact, beside three siblings that all say "Unavailable — … (not a zero)".
    Amber carried the distinction and the words did not.
    """
    tpl = _zero_drop_template()
    # Comments are already stripped, so the section labels occur once each and
    # the visible heading is the anchor.
    section = tpl.split("PR pipeline")[1].split("Items by store")[0]
    assert "pr_pipeline" in section, "the slice must actually contain the part"

    # `x-if`, not `x-show`. Alpine renders a `<template>` only under `x-if`; the
    # same template under `x-show` renders NOTHING, so swapping them deletes the
    # unavailable message entirely while leaving every word of it in the file.
    branches = [chunk.split('">')[0] for chunk in section.split('<template x-if="')[1:]]
    assert len(branches) == 2, (
        f"the part needs exactly two x-if branches — unavailable and not — got {len(branches)}"
    )
    # Order matters as much as presence: an audit swapped the two conditions,
    # which renders "Unavailable — undefined (not a zero)" for a HEALTHY cache
    # and the bare word "unknown" for an unreadable one. Both branches still
    # existed; both words still appeared.
    assert "=== 'unavailable'" in branches[0] and "!== 'unavailable'" in branches[1], (
        "the FIRST branch handles the unavailable envelope and the second is the "
        f"verdict line — got {branches!r}"
    )
    assert "Unavailable —" in section and "(not a zero)" in section, (
        "and must use the same words as the other three parts: an empty read is "
        "not a measurement, and the reader is told so in text rather than in hue"
    )
    # The bare-reason fallback is what made the two indistinguishable. It must be
    # gone from the verdict line, not merely shadowed by the new branch above it.
    assert "?.verdict || $store.genesisDashboard.zeroDropView.pr_pipeline?.reason" not in section, (
        "the verdict line must no longer fall back to a raw reason string — that "
        "fallback is the defect, and leaving it in place keeps it one edit away"
    )


def _js_function_body(js: str, signature: str) -> str:
    """The body of one method in the Alpine store object.

    Crude but anchored: from the signature to the first line that closes the
    method at its own indentation. Enough to ask what a method does and does not
    do, which is the question here — asserting against the whole 5,000-line file
    would be satisfied by the same statement living in a different method.
    """
    start = js.index(signature)
    end = js.index("\n        },", start)
    return js[start:end]


def test_a_retry_in_flight_does_not_erase_the_fault_it_is_retrying():
    """The state machine conflated the PHASE with the FAULT, and the panel paid.

    `startFetch` used to clear `state.error` at the top of every attempt. A panel
    withholding its count on a failing transport therefore un-withheld it for the
    whole duration of each retry — and under a sustained 5xx outage never
    withheld at all: api.js backs off up to ~62s on consecutive 5xx while panels
    poll every 60s, so each in-flight request is superseded by the next before it
    returns, and a superseded non-throwing response returns before `failFetch`.
    The phase never left `refreshing`.

    The fault now survives the retry and only a SUCCESS clears it, which fixes
    both halves at once: the first failure records it, and no later supersession
    can un-record it.
    """
    import pathlib

    js = (
        pathlib.Path(__file__).resolve().parents[2] / "src/genesis/dashboard/webui/js/dashboard.js"
    ).read_text()

    start = _js_function_body(js, "startFetch(name) {")
    assert "state.error = null" not in start, (
        "clearing the error at the start of a retry is the defect — it makes the "
        "fault vanish for the duration of every attempt"
    )
    assert 'state.state = state.lastSuccess ? "refreshing" : "loading"' in start, (
        "the phase is still set; it is the fault that must persist"
    )

    finish = _js_function_body(js, "finishFetch(name) {")
    assert "state.error = null" in finish, (
        "a SUCCESS is the only thing that clears the fault — without this it "
        "would never clear at all"
    )

    fail = _js_function_body(js, "failFetch(name, message) {")
    assert "state.error = message" in fail, "and a failure is what records it"

    refreshing = _js_function_body(js, "refreshFailing(name) {")
    assert "state.error" in refreshing and "state.lastSuccess" in refreshing, (
        "`refreshFailing` is the fault AND the existence of a previous board — "
        "without `lastSuccess` it would also fire for a panel that never loaded, "
        "which has no number to withhold and its own `error` branch"
    )
    assert "state.state" not in refreshing, (
        "it must not read the PHASE: that is the thing it exists to stop asking"
    )


def test_a_superseded_THROWN_request_cannot_mark_a_healthy_transport_as_failing():
    """The token check guarded two of the three exits, and the third now matters.

    `fetchZeroDrop` re-checks its token after the fetch and after `json()`, with
    a comment explaining that calling `failFetch` on a superseded response would
    mark a healthy transport broken. The `catch` had no such check. That was
    cosmetic while nothing acted on the flag; the header badge now withholds its
    count on exactly this signal, so an older request throwing after a newer one
    succeeded would blank a number that is in fact current — and, since the fault
    now persists across retries, it would stay blanked until the next success.
    """
    import pathlib

    js = (
        pathlib.Path(__file__).resolve().parents[2] / "src/genesis/dashboard/webui/js/dashboard.js"
    ).read_text()
    body = js[js.index("async fetchZeroDrop() {") : js.index("async fetchObservations() {")]

    assert body.count("token !== this._zeroDropFetchToken") >= 3, (
        "all three exits — after the fetch, after json(), and in the catch — "
        "must drop a superseded response"
    )
    catch = body[body.index("} catch (e) {") :]
    assert "token !== this._zeroDropFetchToken" in catch, (
        "the catch is the exit that was missing the check"
    )
    assert catch.index("token !== this._zeroDropFetchToken") < catch.index("failFetch"), (
        "and it must come BEFORE failFetch, or it guards nothing"
    )
