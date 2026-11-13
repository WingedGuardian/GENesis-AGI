"""Browser E2E for the read-only Board tab (#4510).

The dashboard is served WITHOUT a runtime (see conftest), so the browser itself
answers ``/api/genesis/board`` (Playwright ``page.route``) with each shape that
``board_status`` can return. That drives the real templates, the real Alpine
store and the real fetch path through every state the tab must render. Runs
locally with the ``.[browser]`` extra; CI skips this directory.
"""

from __future__ import annotations

import json
import re

import pytest

PANEL = "[data-panel=board]"

_HEARTBEAT = {"status": "alive", "last_seen": "2026-11-13T20:00:00+00:00", "age_seconds": 42}
_LATEST = {
    "at": "2026-11-13T20:00:00+00:00",
    "board_state": "ok",
    "mode": "live",
    "paused": False,
    "project": "owner#4",
    "last_error": None,
}
_SUMMARY = {
    "at": "2026-11-13T20:00:00+00:00",
    "age_seconds": 42,
    "project": "owner#4",
    "items_total": 5,
    "by_status": {"Proposed": 3, "In Review": 2},
    "by_genesis": {"none": 5},
    "by_kind": {"Issue": 4, "PullRequest": 1},
    "coverage": {"on_board": 5, "open_in_repo": 9, "other_repo_items": 0},
    "drags": {"in_progress": 0, "newly_logged": 0},
    "writes": {
        "added": 2,
        "status_set": 3,
        "links_set": 0,
        "failed": 0,
        "unresolved": 0,
        "excluded": 0,
        "moved": 0,
        "budget_left": 11,
        "stopped_on": None,
    },
}


@pytest.fixture
def board_page():
    """Like conftest's ``board_page``, but with service workers BLOCKED: the
    dashboard registers ``/sw.js``, and a request made through a service worker
    never reaches ``page.route``, so the tab would fetch from the server."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as exc:  # pragma: no cover - env without a browser binary
            pytest.skip(f"chromium unavailable: {exc}")
        context = browser.new_context(service_workers="block")
        yield context.new_page()
        browser.close()


@pytest.fixture
def board_errors(board_page):
    """Uncaught JS exceptions on the board page (conftest's board_errors rule)."""
    errors: list[str] = []

    def record(exc):
        # The zero-drop panel dereferences its null view on every load (#1995,
        # open); that error is not this tab's and is the only one excused.
        if "reading 'gaps'" not in str(exc):
            errors.append(f"pageerror: {exc}")

    board_page.on("pageerror", record)
    return errors


def _ok(**over):
    body = {
        "status": "ok",
        "heartbeat": dict(_HEARTBEAT),
        "latest": dict(_LATEST),
        "summary": json.loads(json.dumps(_SUMMARY)),
        "pulses_read": 3,
        "scan_limit": 25,
    }
    for key, value in over.items():
        body[key] = value
    return body


def _serve(page, responses):
    """Answer /api/genesis/board with ``responses`` in turn (the last repeats);
    an int is an HTTP error status. Returns the list of requests seen."""
    seen = []

    def handle(route):
        seen.append(route.request.url)
        resp = responses[min(len(seen), len(responses)) - 1]
        if isinstance(resp, int):
            route.fulfill(status=resp, body="error")
        else:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(resp))

    page.route(re.compile(r".*/api/genesis/board(\?.*)?$"), handle)
    return seen


def _open_board(page, base):
    page.goto(f"{base}/genesis#board", wait_until="load")
    page.wait_for_selector(".genesis-dashboard", state="visible", timeout=15_000)
    page.wait_for_selector(PANEL, state="visible", timeout=15_000)


def _panel_text(page, needle, timeout=15_000):
    page.wait_for_function(
        """([sel, needle]) => {
            const el = document.querySelector(sel);
            return !!el && el.innerText.includes(needle);
        }""",
        arg=[PANEL, needle],
        timeout=timeout,
    )
    return page.inner_text(PANEL)


def test_the_nav_button_lands_on_the_board_tab(serve_dashboard, board_page, board_errors):
    base = serve_dashboard(password=None)
    _serve(board_page, [_ok()])
    board_page.goto(f"{base}/genesis", wait_until="load")
    board_page.wait_for_selector(".genesis-dashboard", state="visible", timeout=15_000)
    board_page.locator("nav.genesis-tabs button", has_text="Board").first.click()
    board_page.wait_for_function(
        """() => [...document.querySelectorAll('nav.genesis-tabs button')]
                 .find(b => b.textContent.trim() === 'Board')?.classList.contains('active-tab')""",
        timeout=15_000,
    )
    assert board_page.url.endswith("#board")
    _panel_text(board_page, "on the board")
    assert board_errors == []


def test_a_live_summary_renders_every_part_with_its_denominator(
    serve_dashboard, board_page, board_errors
):
    base = serve_dashboard(password=None)
    _serve(board_page, [_ok()])
    _open_board(board_page, base)
    text = _panel_text(board_page, "on the board")
    for needle in (
        "alive, last pulse 42s ago",
        "mode live",
        "project owner#4",
        "Last error: none",
        "5 on the board",
        "Proposed 3 of 5",
        "In Review 2 of 5",
        "Issue 4 of 5",
        "5 of 9 open issues and PRs",
        "0 in In Progress",
        "added 2, Status set 3, links set 0, failed 0",
        "budget left 11",
    ):
        assert needle in text, (needle, text)
    assert board_errors == []


def test_a_dry_run_says_nothing_was_written(serve_dashboard, board_page, board_errors):
    base = serve_dashboard(password=None)
    body = _ok()
    body["latest"]["mode"] = "propose_only"
    body["summary"]["writes"] = {
        "dry_run": True,
        "would_set_status": 4,
        "would_add_at_most": 1397,
        "would_link": 0,
    }
    _serve(board_page, [body])
    _open_board(board_page, base)
    text = _panel_text(board_page, "dry run")
    assert "dry run, nothing written: would set 4 Status, add at most 1397, link 0" in text
    assert board_errors == []


def test_the_board_off_shows_no_counts(serve_dashboard, board_page, board_errors):
    base = serve_dashboard(password=None)
    body = _ok(summary=None, note="the board is off")
    body["latest"]["mode"] = "off"
    _serve(board_page, [body])
    _open_board(board_page, base)
    text = _panel_text(board_page, "The board is off")
    assert "on the board" not in text and "coverage" not in text.lower()
    assert board_errors == []


@pytest.mark.parametrize(
    "body, needle",
    [
        (
            {"status": "unavailable", "reason": "runtime not bootstrapped"},
            "Board state unavailable: runtime not bootstrapped.",
        ),
        (
            {"status": "error", "reason": "OperationalError: locked"},
            "heartbeats could not be read: OperationalError: locked.",
        ),
        (500, "Couldn't load the board's state"),
    ],
    ids=["unavailable", "heartbeat-read-error", "http-error"],
)
def test_each_failure_shape_is_said_not_shown_as_a_board(
    serve_dashboard, board_page, board_errors, body, needle
):
    base = serve_dashboard(password=None)
    _serve(board_page, [body])
    _open_board(board_page, base)
    text = _panel_text(board_page, needle)
    assert "on the board" not in text
    assert board_errors == []


def test_a_missing_value_shows_as_unknown_never_zero(serve_dashboard, board_page, board_errors):
    base = serve_dashboard(password=None)
    body = _ok()
    body["summary"]["items_total"] = None
    body["summary"]["coverage"] = {"on_board": None, "open_in_repo": 9}
    body["summary"]["writes"] = None
    _serve(board_page, [body])
    _open_board(board_page, base)
    text = _panel_text(board_page, "on the board")
    assert "unknown on the board" in text
    assert "unknown of 9 open issues" in text
    # absent block: show nothing (#4510); innerText applies the CSS uppercase
    assert "writes, last tick" not in text.lower()
    assert board_errors == []


def test_a_summary_older_than_the_pulse_says_so(serve_dashboard, board_page, board_errors):
    base = serve_dashboard(password=None)
    body = _ok()
    body["summary"]["age_seconds"] = 3 * 3600
    _serve(board_page, [body])
    _open_board(board_page, base)
    text = _panel_text(board_page, "did not read the board")
    assert "Counts as of 3h ago (the newest pulse, 42s ago, did not read the board)" in text
    assert board_errors == []


def test_a_failing_refresh_keeps_the_last_board_and_says_so(
    serve_dashboard, board_page, board_errors
):
    base = serve_dashboard(password=None)
    _serve(board_page, [_ok(), 500])
    _open_board(board_page, base)
    _panel_text(board_page, "on the board")
    board_page.evaluate("() => Alpine.store('genesisDashboard').fetchBoard()")
    text = _panel_text(board_page, "Refresh is failing")
    assert "not a current one" in text and "5 on the board" in text
    assert board_errors == []


def test_the_poller_stops_when_the_tab_is_left_and_refetches_on_return(
    serve_dashboard, board_page, board_errors
):
    base = serve_dashboard(password=None)
    seen = _serve(board_page, [_ok()])
    _open_board(board_page, base)
    _panel_text(board_page, "on the board")
    store = "Alpine.store('genesisDashboard')"
    assert board_page.evaluate(f"() => {store}._boardInterval") is not None
    board_page.locator("nav.genesis-tabs button", has_text="Overview").first.click()
    board_page.wait_for_function(f"() => {store}.activeTab === 'overview'", timeout=15_000)
    assert board_page.evaluate(f"() => {store}._boardInterval") is None
    before = len(seen)
    board_page.locator("nav.genesis-tabs button", has_text="Board").first.click()
    board_page.wait_for_function(
        "() => Alpine.store('genesisDashboard').fetchState.board.state !== 'refreshing'",
        timeout=15_000,
    )
    board_page.wait_for_timeout(1000)
    assert len(seen) == before + 1, "re-entering the tab must fetch at once"
    assert board_errors == []


@pytest.mark.parametrize(
    "later",
    [
        {"status": "error", "reason": "OperationalError: locked"},
        {"status": "unavailable", "reason": "restarting"},
    ],
    ids=["error", "unavailable"],
)
def test_a_failed_read_after_a_good_one_keeps_the_board_and_says_so(
    serve_dashboard, board_page, board_errors, later
):
    base = serve_dashboard(password=None)
    _serve(board_page, [_ok(), later])
    _open_board(board_page, base)
    _panel_text(board_page, "on the board")
    board_page.evaluate("() => Alpine.store('genesisDashboard').fetchBoard()")
    text = _panel_text(board_page, "Refresh is failing")
    assert later["reason"] in text and "5 on the board" in text
    assert board_errors == []


def test_paused_never_hides_an_overdue_reconciler(serve_dashboard, board_page, board_errors):
    base = serve_dashboard(password=None)
    body = _ok()
    body["heartbeat"]["status"] = "overdue"
    body["latest"]["paused"] = True
    _serve(board_page, [body])
    _open_board(board_page, base)
    assert "overdue (Genesis paused), last pulse" in _panel_text(board_page, "overdue")
    assert board_errors == []


def test_an_unrecognised_status_is_named_not_left_blank(serve_dashboard, board_page, board_errors):
    base = serve_dashboard(password=None)
    _serve(board_page, [{"status": "weird"}])
    _open_board(board_page, base)
    assert "Unrecognised board state: weird" in _panel_text(board_page, "Unrecognised")
    assert board_errors == []
