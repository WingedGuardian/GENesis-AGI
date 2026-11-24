"""Relayed sessions refuse purchase-like browser controls (browser.py)."""

import os
from dataclasses import replace

import pytest

from genesis import caller_context as cc
from genesis.caller_context import CallerContext, request_caller
from genesis.cc.direct_session import DirectSessionRequest
from genesis.cc.managed_session import fingerprint
from genesis.mcp.health import browser

RELAYED = CallerContext(owner_relay=True)


@pytest.mark.parametrize(
    ("label", "purchase"),
    [
        ("Place order", True),
        ("Place your order", True),
        ("Buy now", True),
        ("Pay $12.99", True),
        ("Proceed to checkout", True),
        ("Submit order", True),
        ("Complete purchase", True),
        ("Start your free trial", True),
        ("Subscribe", True),
        ("Sign in with PayPal", False),
        ("Payment details", False),
        ("Sign in", False),
        ("Add to list", False),
        ("Next", False),
    ],
)
def test_purchase_words(label, purchase):
    assert bool(browser._PURCHASE_WORDS.search(label)) is purchase


class _Target:
    def __init__(self, label="", count=1, error=None):
        self.label, self.n, self.error, self.read = label, count, error, False

    async def count(self):
        return self.n

    async def wait_for(self, state="attached", timeout=None):
        if not self.n:
            raise TimeoutError("not attached")

    async def evaluate(self, script):
        self.read = True
        if self.error:
            raise self.error
        return self.label


class _Page:
    def __init__(self, target=None, focused=""):
        self.target, self.focused = target, focused

    def locator(self, selector):
        return type("L", (), {"first": self.target})()

    async def evaluate(self, script):
        if isinstance(self.focused, Exception):
            raise self.focused
        return self.focused


@pytest.mark.asyncio
async def test_a_relayed_click_on_a_purchase_control_is_refused():
    with request_caller(RELAYED):
        refused = await browser._relay_purchase_refusal(_Page(_Target("Place order")), "#go")
        allowed = await browser._relay_purchase_refusal(_Page(_Target("Sign in")), "#go")
    assert refused["code"] == "relay_purchase_refused" and allowed is None


@pytest.mark.asyncio
async def test_outside_a_relay_nothing_is_read_or_refused():
    target = _Target("Place order")
    with request_caller(CallerContext()):
        assert await browser._relay_purchase_refusal(_Page(target), "#go") is None
        assert await browser._relay_key_refusal(_Page(focused="Pay"), "Enter") is None
        assert await browser._relay_fill_refusal(_Page(target), "#f", "a\nb") is None
    assert not target.read


@pytest.mark.asyncio
async def test_best_effort_a_missing_or_unreadable_control_is_allowed():
    """Functionality first: the check refuses only on a positive match."""
    with request_caller(RELAYED):
        assert await browser._relay_purchase_refusal(_Page(_Target(count=0)), "#x") is None
        assert await browser._relay_purchase_refusal(
            _Page(_Target(error=RuntimeError("detached"))), "#x"
        ) is None
        assert await browser._relay_purchase_refusal(_Page(_Target(None)), "iframe") is None
        assert await browser._relay_key_refusal(_Page(focused=RuntimeError("gone")), "Enter") is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("key", "focused", "refused"),
    [
        ("Enter", "email Place order", True),
        ("Control+Enter", "Place order", True),
        ("Space", "Buy now", True),
        ("\n", "Place order", True),
        ("\r", "Place order", True),
        ("Enter", "Search", False),
        ("a", "Place order", False),
        ("Tab", "Place order", False),
    ],
)
async def test_a_relayed_submitting_key_press_on_a_purchase_form_is_refused(key, focused, refused):
    with request_caller(RELAYED):
        result = await browser._relay_key_refusal(_Page(focused=focused), key)
    assert (result is not None) is refused


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "submits", "refused"),
    [
        ("line one\nline two", "Place order", True),
        ("line one\nline two", "Search", False),
        ("line one\nline two", "", False),
        ("line one\nline two", RuntimeError("detached"), False),
        ("one line", "Place order", False),
    ],
)
async def test_a_relayed_line_break_is_refused_only_where_it_submits_a_purchase(value, submits, refused):
    target = _Target(submits) if not isinstance(submits, Exception) else _Target(error=submits)
    with request_caller(RELAYED):
        result = await browser._relay_fill_refusal(_Page(target), "#f", value)
    assert (result is not None) is refused


def test_the_flag_is_read_from_the_environment_and_cleared_for_a_shared_service(monkeypatch):
    monkeypatch.setenv(cc.OWNER_RELAY_ENV, "1")
    assert cc.caller_context_from_env().owner_relay is True
    monkeypatch.setenv(cc.OWNER_RELAY_ENV, "0")
    assert cc.caller_context_from_env().owner_relay is False
    assert cc.OWNER_RELAY_ENV in cc.IDENTITY_ENV_VARS
    # A caller of unknown scope is treated as relayed: purchases are refused.
    assert cc._LOST_SCOPE_CALLER.owner_relay is True


def _managed(**changes):
    return DirectSessionRequest(
        prompt="p", profile="interact", source_tag="agent_api",
        caller_context="agent_api:x", timeout_s=60, managed=True, notify=False, **changes,
    )


def test_owner_relay_is_validated_and_kept_out_of_the_default_fingerprint():
    plain = _managed()
    relayed = replace(plain, owner_relay=True)
    assert fingerprint(plain) != fingerprint(relayed)
    for bad in ({"owner_relay": 1}, {"owner_relay": True, "managed": False}):
        with pytest.raises(ValueError):
            replace(plain, **bad) if "managed" not in bad else DirectSessionRequest(
                prompt="p", owner_relay=True
            )


def test_only_a_relayed_request_sets_the_flag_on_its_invocation():
    from tests.test_cc.test_direct_session_profiles import _make_runner

    runner = _make_runner()
    plain = runner._build_invocation(_managed(), "s")
    relayed = runner._build_invocation(_managed(owner_relay=True), "s")
    assert not (plain.env_overrides or {}).get(cc.OWNER_RELAY_ENV)
    assert relayed.env_overrides == {cc.OWNER_RELAY_ENV: "1"}
    assert os.environ.get(cc.OWNER_RELAY_ENV) != "1"


def test_the_health_mcp_entry_has_no_env_block_to_replace_the_inherited_flag():
    """The flag reaches the browser tool because stdio MCP servers inherit the
    session environment; an explicit env block could override or drop it."""
    import json
    from pathlib import Path

    template = json.loads(Path("config/mcp.json.template").read_text())
    servers = template.get("mcpServers", template)
    assert "env" not in servers["genesis-health"]


# --- the tools themselves, so removing a call site fails a test ---


def _tool_page(label="Place order", focused="Place order", submit=""):
    from unittest.mock import AsyncMock, MagicMock

    page = MagicMock()
    page.url = "https://shop.example/checkout"
    page.is_closed = MagicMock(return_value=False)
    target = MagicMock()
    target.count = AsyncMock(return_value=1)
    target.evaluate = AsyncMock(
        side_effect=lambda script: submit if script == browser._NEWLINE_SUBMIT_LABEL_JS else label
    )
    page.locator = MagicMock(return_value=MagicMock(first=target))
    page.evaluate = AsyncMock(return_value=focused)
    page.keyboard = MagicMock(press=AsyncMock())
    return page


@pytest.fixture
def tool_page(monkeypatch):
    monkeypatch.setattr(browser, "_is_remote_active", lambda: False)
    monkeypatch.setattr(browser, "_detach_dead_remote", lambda: None)
    monkeypatch.setattr(browser, "_touch", lambda: None)

    def use(page):
        monkeypatch.setattr(browser, "_active_page", page)
        return page

    return use


@pytest.mark.asyncio
async def test_browser_click_refuses_a_purchase_before_clicking(tool_page, monkeypatch):
    from unittest.mock import AsyncMock

    click = AsyncMock(return_value={"clicked": "#go"})
    monkeypatch.setattr(browser, "_click_and_follow", click)
    tool_page(_tool_page())
    with request_caller(RELAYED):
        result = await browser._impl_browser_click("#go")
    assert result["code"] == "relay_purchase_refused"
    click.assert_not_awaited()
    with request_caller(CallerContext()):
        await browser._impl_browser_click("#go")
    click.assert_awaited_once()


@pytest.mark.asyncio
async def test_browser_press_key_refuses_enter_on_a_purchase_form(tool_page):
    page = tool_page(_tool_page())
    with request_caller(RELAYED):
        result = await browser._impl_browser_press_key("Enter")
    assert result["code"] == "relay_purchase_refused"
    page.keyboard.press.assert_not_awaited()


@pytest.mark.asyncio
async def test_browser_press_key_checks_before_every_press(tool_page):
    page = tool_page(_tool_page())
    page.evaluate.side_effect = ["Continue", "Place order"]
    with request_caller(RELAYED):
        result = await browser._impl_browser_press_key("Enter", count=2)
    assert result["code"] == "relay_purchase_refused"
    assert page.keyboard.press.await_count == 1


@pytest.mark.asyncio
async def test_browser_fill_refuses_a_line_break_that_submits_a_purchase(tool_page, monkeypatch):
    from unittest.mock import AsyncMock

    fill = AsyncMock(return_value={"filled": "#f"})
    monkeypatch.setattr(browser, "_fill", fill)
    tool_page(_tool_page(label="About you", submit="Place order"))
    with request_caller(RELAYED):
        result = await browser._impl_browser_fill("#f", "a\nb")
    assert result["code"] == "relay_purchase_refused"
    fill.assert_not_awaited()
    tool_page(_tool_page(label="About you", submit="Send message"))
    with request_caller(RELAYED):
        await browser._impl_browser_fill("#f", "a\nb")
    fill.assert_awaited_once()


# --- the page scripts, in a real DOM (skipped where Chromium is absent) ---


@pytest.fixture(scope="module")
def real_page():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright is not installed")
    with sync_playwright() as p:
        try:
            chromium = p.chromium.launch(headless=True)
        except Exception as e:  # noqa: BLE001 - no browser binary here
            pytest.skip(f"no Playwright Chromium ({type(e).__name__})")
        yield chromium.new_page()
        chromium.close()


@pytest.mark.parametrize(
    ("html", "selector", "purchase"),
    [
        ("<button><span>Blue shoe</span><span>Buy now $19</span></button>", "button", True),
        ("<input type=submit value='Place order'>", "input", True),
        ("<a href=#><img alt='Checkout'></a>", "img", True),
        ("<span id=l>Pay</span><button aria-labelledby=l>&rarr;</button>", "button", True),
        ("<button>Sign in with PayPal</button>", "button", False),
        ("<button>" + "x " * 1500 + "Pay now</button>", "button", True),
    ],
)
def test_the_label_script_reads_what_a_shopper_sees(real_page, html, selector, purchase):
    real_page.set_content(html)
    text = real_page.locator(selector).first.evaluate(browser._CONTROL_LABEL_JS)
    assert bool(browser._PURCHASE_WORDS.search(text)) is purchase


def test_the_focus_script_reads_the_form_submit_shadow_roots_and_frames(real_page):
    real_page.set_content(
        "<form><input id=q><button>Place order</button></form>"
    )
    real_page.focus("#q")
    assert browser._PURCHASE_WORDS.search(real_page.evaluate(browser._FOCUSED_LABEL_JS))
    real_page.set_content("<div id=h></div>")
    real_page.evaluate(
        "() => { const r = document.getElementById('h').attachShadow({mode: 'open'});"
        " r.innerHTML = '<button>Pay now</button>'; r.querySelector('button').focus(); }"
    )
    assert browser._PURCHASE_WORDS.search(real_page.evaluate(browser._FOCUSED_LABEL_JS))
    real_page.set_content(
        "<form><textarea id=t></textarea><input id=i><button>Place order</button></form>"
    )
    textarea = real_page.locator("#t").first.evaluate(browser._NEWLINE_SUBMIT_LABEL_JS)
    field = real_page.locator("#i").first.evaluate(browser._NEWLINE_SUBMIT_LABEL_JS)
    assert textarea == "" and browser._PURCHASE_WORDS.search(field)


def test_the_click_fallbacks_stay_on_and_the_key_fallback_is_checked():
    """Functionality first: both fallbacks run for a relayed session too; the
    keyboard one is checked like a key press before it presses."""
    import inspect

    source = inspect.getsource(browser._stealth_click)
    assert "if _relayed():" not in source
    assert "if await _relay_key_refusal(page, key):" in source
    assert "if await _click_in_shadow_dom(page, selector):" in source


def test_place_a_bid_and_image_submits_are_covered(real_page):
    assert browser._PURCHASE_WORDS.search("Place a bid")
    real_page.set_content(
        "<form><input id=q><input type=image alt='Place order' src='data:,'></form>"
    )
    real_page.focus("#q")
    assert browser._PURCHASE_WORDS.search(real_page.evaluate(browser._FOCUSED_LABEL_JS))




@pytest.mark.asyncio
async def test_browser_fill_refuses_typing_into_a_purchase_control(tool_page, monkeypatch):
    from unittest.mock import AsyncMock

    fill = AsyncMock(return_value={"filled": "#f"})
    monkeypatch.setattr(browser, "_fill", fill)
    tool_page(_tool_page(label="Place order"))
    with request_caller(RELAYED):
        result = await browser._impl_browser_fill("#f", "anything")
    assert result["code"] == "relay_purchase_refused"
    fill.assert_not_awaited()


def test_the_form_submit_follows_tree_order_and_dropdowns_are_free(real_page):
    real_page.set_content(
        "<form><input id=q><input type=image alt='Pay now' src='data:,'>"
        "<button>Save for later</button></form>"
    )
    real_page.focus("#q")
    assert browser._PURCHASE_WORDS.search(real_page.evaluate(browser._FOCUSED_LABEL_JS))
    real_page.set_content(
        "<form><select id=s><option>Small</option></select><button>Place order</button></form>"
    )
    real_page.focus("#s")
    assert not browser._PURCHASE_WORDS.search(real_page.evaluate(browser._FOCUSED_LABEL_JS))


def test_the_enclosing_buttons_own_labels_are_read(real_page):
    real_page.set_content(
        "<span id=l>Place order</span><button aria-labelledby=l><span id=i>&rarr;</span></button>"
    )
    text = real_page.locator("#i").first.evaluate(browser._CONTROL_LABEL_JS)
    assert browser._PURCHASE_WORDS.search(text)


@pytest.mark.asyncio
async def test_a_refused_keyboard_fallback_stops_the_click(monkeypatch):
    """The fallback's refusal ends the click: neither the press nor the shadow-DOM
    click runs, and the caller gets the refusal (#5014 review)."""
    from unittest.mock import AsyncMock, MagicMock

    page = MagicMock()
    stealth = MagicMock(wait_for=AsyncMock(side_effect=RuntimeError("stealth click failed")))
    page.locator = MagicMock(return_value=MagicMock(first=stealth))
    el = MagicMock(focus=AsyncMock(), evaluate=AsyncMock(side_effect=["button", ""]))
    page.wait_for_selector = AsyncMock(return_value=el)
    page.click = AsyncMock(side_effect=RuntimeError("Timeout 10000ms exceeded"))
    page.keyboard = MagicMock(press=AsyncMock())
    page.evaluate = AsyncMock(return_value="Place order")
    shadow = AsyncMock(return_value=True)
    monkeypatch.setattr(browser, "_click_in_shadow_dom", shadow)
    monkeypatch.setattr(browser, "_is_camoufox_active", lambda: True)
    monkeypatch.setattr(browser, "_blocked_click", AsyncMock(return_value=None))
    monkeypatch.setattr(browser, "_click_was_sent", lambda err, selector: False)
    with request_caller(RELAYED), pytest.raises(browser.RelayPurchaseRefused):
        await browser._stealth_click(page, "#go", timeout=10)
    page.keyboard.press.assert_not_awaited()
    shadow.assert_not_awaited()

