"""payment_context's page scripts, run in a real (headless Chromium) DOM.

The flow tests fake the page, so they cannot see what a script does. These run
the scripts themselves on local HTML (set_content: no network, no profile, no
display). Skipped where Playwright's Chromium is not installed (as in CI).
"""

from __future__ import annotations

from urllib.parse import quote

import pytest

from genesis.mcp.health import payment_context as pc


@pytest.fixture(scope="module")
def page():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright is not installed")
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001 - no browser binary here
            pytest.skip(f"no Playwright Chromium ({type(e).__name__})")
        ctx = browser.new_context()
        ctx.add_init_script(pc.MASK_INIT_JS)
        yield ctx.new_page()
        browser.close()


def _el(page, html: str, sel: str):
    page.set_content(html)
    return page.query_selector(sel)


@pytest.mark.parametrize(
    ("html", "kind", "tag"),
    [
        ('<input id=x type=password autocomplete="cc-csc">', "cvc", "input"),
        ('<input id=x type=password autocomplete="cc-number">', "number", ""),
        ('<input id=x autocomplete="cc-number">', "number", "input"),
        ("<label for=x>Card number</label><input id=x type=tel>", "number", "input"),
        ('<input id=x type=hidden autocomplete="cc-csc">', "cvc", ""),
    ],
)
def test_card_field_js(page, html, kind, tag):
    el = _el(page, html, "#x")
    assert el.evaluate(pc.CARD_FIELD_JS, kind) == tag


@pytest.mark.parametrize(
    ("html", "button", "get"),
    [
        ("<form method=post><button id=x>Pay $5</button></form>", True, False),
        ("<form method=get><button id=x>Pay $5</button></form>", True, True),
        ("<form method=post><button id=x formmethod=get>Pay $5</button></form>", True, True),
        ("<button id=x type=reset>Pay $5</button>", False, False),
        ("<input id=x type=submit value='Pay $5'>", True, False),
        # GET only by default (no method attribute): a script-handled checkout,
        # left to the network guard rather than refused before the tap.
        (
            "<form><input name=cardNumber autocomplete=cc-number>"
            "<button id=x>Pay $5</button></form>",
            True,
            False,
        ),
        # An invalid declared method falls back to GET, so it is declared GET.
        ("<form method=GTE><button id=x>Pay $5</button></form>", True, True),
        ("<form method=get><button id=x formmethod=post>Pay $5</button></form>", True, False),
        ("<form><button id=x formmethod=bogus>Pay $5</button></form>", True, True),
        # A form control named after a form property shadows it (legacy named
        # access): the method must still be read off the form itself.
        ("<form method=get><input name=method><button id=x>Pay $5</button></form>", True, True),
        (
            "<form method=get><input name=hasAttribute><button id=x>Pay $5</button></form>",
            True,
            True,
        ),
    ],
)
def test_pay_js(page, html, button, get):
    info = (_el(page, html, "#x")).evaluate(pc.PAY_JS)
    assert (info["button"], info["get"], info["label"].strip()) == (button, get, "Pay $5")


@pytest.mark.parametrize(
    ("html", "dget"),
    [
        ("<form><button id=x>Pay</button></form>", True),
        ("<form method=post><button id=x>Pay</button></form>", False),
        ("<form method=get><button id=x>Pay</button></form>", False),  # declared, not default
        ("<form><button id=x formmethod=post>Pay</button></form>", False),
        ("<button id=x>Pay</button>", False),  # no form at all
    ],
)
def test_pay_js_reports_a_get_by_default(page, html, dget):
    assert (_el(page, html, "#x")).evaluate(pc.PAY_JS)["dget"] is dget


@pytest.mark.parametrize(
    ("html", "sent"),
    [
        ("<form><input id=f name=cvc><button id=p>Pay</button></form>", True),
        ("<form><input id=f><button id=p>Pay</button></form>", False),  # unnamed
        ("<form><input id=f name=cvc disabled><button id=p>Pay</button></form>", False),
        ("<form><input id=f name=cvc></form><form><button id=p>Pay</button></form>", False),
        ("<input id=f name=cvc form=g><form id=g><button id=p>Pay</button></form>", True),
        ("<input id=f name=cvc><button id=p>Pay</button>", False),  # no forms
    ],
)
def test_sent_with_pay_js(page, html, sent):
    page.set_content(html)
    f, p = page.query_selector("#f"), page.query_selector("#p")
    assert f.evaluate(pc.SENT_WITH_PAY_JS, p) is sent


def test_the_mask_survives_a_page_script_that_removes_it_during_input(page):
    page.set_content(
        "<input id=x><script>document.getElementById('x').addEventListener('input', () => {"
        " document.getElementById('__genesis_payment_mask__').remove();"
        " document.documentElement.removeAttribute('style'); });</script>"
    )
    assert page.evaluate(pc._MASK_CHECK_JS) is True
    page.fill("#x", "1")
    # By the time any later task (a paint, this evaluate) runs, the observer has restored it.
    assert page.evaluate(pc._MASK_CHECK_JS) is True
    assert page.evaluate("() => getComputedStyle(document.documentElement).opacity") == "0"


def test_extra_value_js_reads_a_select_by_value_and_label(page):
    el = _el(page, "<select id=x><option value=FR>France</option></select>", "#x")
    assert el.evaluate(pc.EXTRA_VALUE_JS) == ["FR", "France"]


def test_the_mask_survives_a_media_attribute_on_its_style(page):
    page.set_content("<input id=x>")
    page.evaluate("() => document.getElementById('__genesis_payment_mask__').media = 'print'")
    assert page.evaluate(pc._MASK_CHECK_JS) is True


# The top layer (a modal dialog, a popover, a fullscreen element and their
# backdrops) paints outside the root's opacity group, so the root mask alone
# does not hide it: the mask covers it explicitly, and the check verifies it.
_TOP_LAYER_OPACITY = (
    "(sel) => { const e = document.querySelector(sel);"
    " return [getComputedStyle(e).opacity, getComputedStyle(e, '::backdrop').opacity]; }"
)


def test_a_modal_dialog_is_masked(page):
    page.set_content(
        "<dialog id=d><p>order</p></dialog>"
        "<script>document.getElementById('d').showModal()</script>"
    )
    assert page.evaluate(_TOP_LAYER_OPACITY, "#d") == ["0", "0"]
    assert page.evaluate(pc._MASK_CHECK_JS) is True


def test_an_open_popover_is_masked(page):
    page.set_content(
        "<div id=p popover>order</div><script>document.getElementById('p').showPopover()</script>"
    )
    assert page.evaluate(_TOP_LAYER_OPACITY, "#p")[0] == "0"
    assert page.evaluate(pc._MASK_CHECK_JS) is True


def test_the_mask_wins_over_a_page_forcing_a_dialog_visible(page):
    # Inline important from the page, and a page rule marked important: the
    # mask re-asserts an inline important when the dialog opens, which wins.
    page.set_content(
        "<style>#d:modal { opacity: 1 !important }</style>"
        "<dialog id=d style='opacity: 1 !important'><p>order</p></dialog>"
        "<script>document.getElementById('d').showModal()</script>"
    )
    assert page.evaluate(_TOP_LAYER_OPACITY, "#d")[0] == "0"
    assert page.evaluate(pc._MASK_CHECK_JS) is True


def test_the_mask_wins_over_a_page_forcing_a_popover_visible(page):
    # Opening a popover changes no attribute; the toggle events catch it. A real
    # navigation, not set_content: document.open() drops a document's listeners.
    html = "<style>#p:popover-open { opacity: 1 !important }</style><div id=p popover>order</div>"
    page.goto("data:text/html," + quote(html))
    page.evaluate("() => document.getElementById('p').showPopover()")
    page.wait_for_timeout(50)
    assert page.evaluate(_TOP_LAYER_OPACITY, "#p")[0] == "0"
    assert page.evaluate(pc._MASK_CHECK_JS) is True


def test_a_backdrop_the_page_forces_visible_fails_the_check(page):
    # A pseudo-element takes no inline style: a page rule that out-ranks the
    # mask's is caught by the check, which then fails closed.
    page.set_content(
        "<style>html body dialog#d::backdrop { opacity: 1 !important }</style>"
        "<dialog id=d><p>order</p></dialog>"
        "<script>document.getElementById('d').showModal()</script>"
    )
    assert page.evaluate(pc._MASK_CHECK_JS) is False


def test_the_view_transition_overlay_is_masked(page):
    page.set_content("<p>x</p>")
    assert (
        page.evaluate(
            "() => getComputedStyle(document.documentElement, '::view-transition').opacity"
        )
        == "0"
    )
    assert page.evaluate(pc._MASK_CHECK_JS) is True


def test_a_view_transition_the_page_forces_visible_fails_the_check(page):
    page.set_content("<style>html:root::view-transition { opacity: 1 !important }</style><p>x</p>")
    assert page.evaluate(pc._MASK_CHECK_JS) is False


def test_the_page_background_is_cleared(page):
    page.set_content("<body style='background: red'><p>x</p></body>")
    assert page.evaluate("() => getComputedStyle(document.body).backgroundColor") in (
        "transparent",
        "rgba(0, 0, 0, 0)",
    )
    assert page.evaluate(pc._MASK_CHECK_JS) is True


def test_a_background_the_page_forces_fails_the_check(page):
    page.set_content("<style>html body#b { background: red !important }</style><body id=b>x</body>")
    assert page.evaluate(pc._MASK_CHECK_JS) is False


def test_the_mask_absent_probe(page):
    page.set_content("<p>x</p>")
    assert page.evaluate(pc._MASK_ABSENT_JS) is False
    page.evaluate("() => document.getElementById('__genesis_payment_mask__').remove()")
    # The observer puts it back before any later task can look.
    assert page.evaluate(pc._MASK_ABSENT_JS) is False
