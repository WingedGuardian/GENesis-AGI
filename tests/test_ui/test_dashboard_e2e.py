"""Browser-level UI E2E tests for the Genesis dashboard.

These drive a REAL headless Chromium (Playwright) against the dashboard Flask
app served on an ephemeral localhost port — coverage the in-process
``test_client`` suites (tests/test_dashboard/) cannot give: the Alpine.js app
actually boots, static assets actually load, tab routing actually runs, and the
cookie login round-trips in a real browser.

Runtime-only. Playwright + Chromium live in the ``.[browser]`` extra and are NOT
installed in CI, so this module skips there via ``importorskip`` (in conftest);
the directory is also ``--ignore``d by CI (.github/workflows/ci.yml). Browser
E2E is a local / on-demand harness, never part of the headless CI run:

    pip install -e '.[browser]' && playwright install chromium
    pytest tests/test_ui/ -v

Scope: the dashboard app is built WITHOUT a live runtime (see conftest), so
data-backed routes degrade to 503 and these tests assert the UI *shell*,
routing, and auth behaviour — not live data. Data-populated assertions would
need a booted runtime and belong in a separate, heavier harness.
"""

from __future__ import annotations

# Stable selectors, derived from templates/partials/chrome/header.html:
#   <nav class="genesis-tabs"> with text-labelled <button>s; the active button
#   carries the ``active-tab`` class bound to $store.genesisDashboard.activeTab
#   (default "overview", hash-routed).
TAB_BAR = "nav.genesis-tabs"


def _open_dashboard(page, base: str):
    """Load /genesis and wait for the Alpine shell to un-hide (loading=false)."""
    resp = page.goto(f"{base}/genesis", wait_until="load")
    assert resp is not None and resp.status == 200, f"GET /genesis: {resp}"
    # .genesis-dashboard is x-show="!loading"; visible only once onOpen() settles.
    page.wait_for_selector(".genesis-dashboard", state="visible", timeout=15_000)
    return resp


def test_dashboard_shell_renders(serve_dashboard, browser_page, page_errors):
    """The dashboard boots, titles correctly, and shows its tab bar + tabs."""
    base = serve_dashboard(password=None)
    _open_dashboard(browser_page, base)

    assert browser_page.title() == "Genesis Dashboard"

    browser_page.wait_for_selector(TAB_BAR, state="visible", timeout=15_000)
    # A representative slice of the known tabs must be present and labelled.
    for label in ("Overview", "Memory", "Files", "Configuration"):
        assert browser_page.locator(f"{TAB_BAR} button", has_text=label).count() >= 1, (
            f"tab button missing: {label}"
        )

    assert page_errors == [], f"uncaught JS errors on load: {page_errors}"


def test_default_tab_is_overview(serve_dashboard, browser_page, page_errors):
    """On first load the Overview tab is the active one."""
    base = serve_dashboard(password=None)
    _open_dashboard(browser_page, base)

    # Condition-based (consistent with test_tab_switch): the Overview button
    # carries active-tab once Alpine binds the default activeTab="overview".
    browser_page.wait_for_function(
        """() => {
            const b = [...document.querySelectorAll('nav.genesis-tabs button')]
              .find(el => el.textContent.trim() === 'Overview');
            return !!b && b.classList.contains('active-tab');
        }""",
        timeout=15_000,
    )
    assert page_errors == [], f"uncaught JS errors: {page_errors}"


def test_tab_switch_updates_active_tab(serve_dashboard, browser_page, page_errors):
    """Clicking a tab runs the real hash-router → the clicked tab goes active."""
    base = serve_dashboard(password=None)
    _open_dashboard(browser_page, base)

    browser_page.locator(f"{TAB_BAR} button", has_text="Files").first.click()

    # Condition-based wait (no arbitrary sleep): the Files button gains active-tab.
    browser_page.wait_for_function(
        """() => {
            const b = [...document.querySelectorAll('nav.genesis-tabs button')]
              .find(el => el.textContent.trim() === 'Files');
            return !!b && b.classList.contains('active-tab');
        }""",
        timeout=15_000,
    )
    assert "files" in browser_page.url  # hash-routed to #files
    assert page_errors == [], f"uncaught JS errors on tab switch: {page_errors}"


def test_login_gate_redirects_then_authenticates(serve_dashboard, browser_page, page_errors):
    """With a password set, /genesis gates to login; correct pw round-trips in."""
    password = "e2e-test-pw"  # noqa: S105 - synthetic test credential
    base = serve_dashboard(password=password)

    browser_page.goto(f"{base}/genesis", wait_until="load")
    assert "/genesis/login" in browser_page.url, "unauthed load should hit login"

    browser_page.wait_for_selector("#pw", timeout=15_000)
    browser_page.fill("#pw", password)
    browser_page.click("#btn")

    browser_page.wait_for_url(f"{base}/genesis", timeout=15_000)
    browser_page.wait_for_selector(".genesis-dashboard", state="visible", timeout=15_000)
    assert page_errors == [], f"uncaught JS errors during login: {page_errors}"


def test_wrong_password_is_rejected(serve_dashboard, browser_page):
    """A bad password keeps the user on the login page (401 handled client-side)."""
    base = serve_dashboard(password="the-right-one")

    browser_page.goto(f"{base}/genesis", wait_until="load")
    browser_page.wait_for_selector("#pw", timeout=15_000)
    browser_page.fill("#pw", "the-wrong-one")
    browser_page.click("#btn")

    # Deterministic signal instead of a sleep: the login JS writes a non-empty
    # #err on a rejected password and never navigates.
    browser_page.wait_for_function(
        "() => { const e = document.querySelector('#err');"
        " return !!e && e.textContent.trim().length > 0; }",
        timeout=15_000,
    )
    assert "/genesis/login" in browser_page.url
    # And a fresh load still gates — no session was created by the bad login.
    browser_page.goto(f"{base}/genesis", wait_until="load")
    assert "/genesis/login" in browser_page.url, "bad login must not authenticate"
