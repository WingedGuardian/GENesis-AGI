"""The payment-tab guard (genesis.mcp.health.payment_tab and its browser.py hooks).

The reader matrix is the plan's table (arming state x reader). Pages are mocks;
the live behaviour (Back, iframes, the mask on screen, closing) is
test_payment_tab_live.py. Card values below are Stripe's public test numbers.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock, patch
from urllib.parse import urlsplit

import pytest

from genesis import env
from genesis.browser.types import BrowserLayer
from genesis.mcp.health import browser, payment_tab
from genesis.mcp.health.payment_tab import HIDDEN, TABS, PaymentTabError
from genesis.security.card_redaction import CARD_FILTER
from genesis.security.card_shapes import REDACTED

PAN = "4242424242424242"
OTHER_PAN = "4000002500003155"
EXP = "12/34"
CVC = "739"
NAME = "Pat Tester"
FILLED = (PAN, EXP, CVC, NAME)
# Every form of a filled value that must never leave the harness.
SECRETS = (PAN, "4242 4242 4242 4242", EXP, "1234", CVC, "Tester")

SNAPSHOT = (
    '- heading "Checkout" [level=1]\n'
    f'- textbox "Card number": {PAN}\n'
    f'- textbox "Expiry": {EXP}\n'
    f'- spinbutton "CVC": "{CVC}"\n'
    f'- textbox "Name": {NAME}\n'
    '- searchbox "Find": shoes\n'
    '- combobox "Month":\n'
    '  - option "01"\n'
    '  - option "12" [selected]\n'
    f"- paragraph: Paying with card {CVC} of {NAME}\n"
    "- iframe"
)
STATES = ("awaiting_tap", "filling", "armed", "crashed")
# Read at import, before tests/conftest.py points it at a tmp dir.
_REAL_PENDING_PATH = getattr(env, "payment_tab_pending_path", None)


def _no_secret(blob) -> None:
    text = json.dumps(blob, default=str) if not isinstance(blob, str) else blob
    for s in SECRETS:
        assert s not in text, f"a filled value reached the output: {s[:2]}..."


def _page(url="https://shop.example/checkout", snapshot=SNAPSHOT, title="Checkout"):
    """A Playwright Page double whose event handlers can be fired."""
    page = MagicMock()
    page.handlers = {}

    def on(event, fn):
        page.handlers.setdefault(event, []).append(fn)

    page.on.side_effect = on
    page.once.side_effect = on
    page.is_closed.return_value = False
    page.url = url
    frame = MagicMock(url=url)
    frame.evaluate = AsyncMock(return_value=True)
    page.frames = [frame]
    page.title = AsyncMock(return_value=title)
    page.locator.return_value.aria_snapshot = AsyncMock(return_value=snapshot)
    page.evaluate = AsyncMock(return_value="ran")
    page.screenshot = AsyncMock()
    page.keyboard.press = AsyncMock()
    page.set_input_files = AsyncMock()
    page.add_init_script = AsyncMock()
    page.goto = AsyncMock()

    async def close(**_kw):
        page.is_closed.return_value = True
        for fn in page.handlers.get("close", []):
            fn(page)

    page.close = AsyncMock(side_effect=close)
    page.context.pages = [page]
    return page


def _fire(page, event):
    for fn in list(page.handlers.get(event, [])):
        fn(page)


async def _arm_in(state: str, page):
    await payment_tab.arm(page)
    if state in ("filling", "armed", "crashed"):
        payment_tab.begin_fill(page, FILLED)
    if state in ("armed", "crashed"):
        payment_tab.mark_filled(page)
    if state == "crashed":
        _fire(page, "crash")


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    TABS.reset()
    TABS.prefs_pinned = True  # a Camoufox launched with card fill on
    monkeypatch.setattr(browser, "_active_page", None)
    monkeypatch.setattr(browser, "_stealth_page", None)
    monkeypatch.setattr(browser, "_stealth_cm", None)
    monkeypatch.setattr(browser, "_stealth_browser", None)
    monkeypatch.setattr(browser, "_page", None)
    monkeypatch.setattr(browser, "_browser_lock", asyncio.Lock())
    monkeypatch.setattr(browser, "_TS_LOG_PATH", tmp_path / "ts.log")
    monkeypatch.setattr(browser, "_SCREENSHOT_DIR", tmp_path)
    monkeypatch.setattr(browser, "_NEW_TAB_WAIT_S", 0.01)
    monkeypatch.setattr(browser, "_human_delay", AsyncMock())
    monkeypatch.setattr(browser, "_start_idle_watcher", lambda: None)
    monkeypatch.setattr(browser, "_layer_last_used", {})
    monkeypatch.setattr(browser, "_opened_by", {})
    # Storage clears never launch anything here; tests that care replace it.
    monkeypatch.setattr(payment_tab, "clear_site_storage", AsyncMock(return_value=True))
    yield
    TABS.reset()


def _drive(page):
    """Make ``page`` the Camoufox page the tools drive."""
    browser._active_page = page
    browser._stealth_page = page


# ---------------------------------------------------------------------------
# Snapshot redaction, steps 1-4
# ---------------------------------------------------------------------------


class TestHideFormValues:
    def test_every_value_bearing_control_is_hidden_and_options_dropped(self):
        out = payment_tab.hide_form_values(SNAPSHOT)
        assert f'- textbox "Card number": {HIDDEN}' in out
        assert f'- spinbutton "CVC": {HIDDEN}' in out
        assert f'- searchbox "Find": {HIDDEN}' in out
        assert f'- combobox "Month": {HIDDEN}' in out
        assert "option" not in out
        assert '- heading "Checkout" [level=1]' in out  # other lines stay
        for v in (PAN, EXP, '"739"', NAME):
            assert f": {v}" not in out

    @pytest.mark.parametrize(
        "line",
        [
            '- textbox "Card: number" [disabled]: 4242',
            "- textbox: 4242",
            '- textbox "a \\"q\\" b": 4242',
            '  - spinbutton "x" [ref=e5]: 4242',
            '- textbox "weird" oddtail 4242',
        ],
    )
    def test_names_with_colons_quotes_and_odd_tails(self, line):
        out = payment_tab.hide_form_values(line)
        assert "4242" not in out and HIDDEN in out

    def test_nested_lines_under_a_control_are_dropped(self):
        text = '- textbox "Notes": |-\n    line one 739\n    line two\n- button "Pay"'
        assert payment_tab.hide_form_values(text) == f'- textbox "Notes": {HIDDEN}\n- button "Pay"'

    def test_a_listbox_option_is_dropped_with_its_children(self):
        text = '- listbox "Saved":\n  - option "Visa 739":\n    - text: 739\n- button "Pay"'
        assert payment_tab.hide_form_values(text) == '- listbox "Saved":\n- button "Pay"'


class TestRedactSnapshot:
    @pytest.mark.asyncio
    async def test_armed_page_hides_controls_then_tokens_then_cards(self, monkeypatch):
        page = _page()
        await _arm_in("armed", page)
        seen = []
        real = payment_tab.redact

        def card_redact(text):
            seen.append(text)
            return real(text)

        monkeypatch.setattr(payment_tab, "redact", card_redact)
        out = TABS.redact_snapshot(page, SNAPSHOT + f"\n- text: {OTHER_PAN}", True, TABS.epoch)
        # card_shapes runs last: its input already has the controls and the
        # matching tokens hidden.
        assert len(seen) == 1 and PAN not in seen[0] and CVC not in seen[0]
        assert REDACTED in out and OTHER_PAN not in out
        _no_secret(out)
        assert f"Paying with card {HIDDEN} of {HIDDEN} {HIDDEN}" in out

    @pytest.mark.asyncio
    async def test_a_page_armed_during_the_read_counts_as_armed(self):
        page = _page()
        epoch = TABS.epoch
        await payment_tab.arm(page)  # lands while the read was in flight
        assert HIDDEN in TABS.redact_snapshot(page, SNAPSHOT, False, epoch)

    @pytest.mark.asyncio
    async def test_an_arming_that_began_and_ended_during_the_read_still_counts(self):
        page, other = _page(), _page()
        epoch = TABS.epoch
        await payment_tab.arm(other)
        payment_tab.disarm(other)
        assert HIDDEN in TABS.redact_snapshot(page, SNAPSHOT, False, epoch)

    def test_unarmed_page_keeps_its_values(self):
        page = _page()
        out = TABS.redact_snapshot(page, '- textbox "Email": a@b.example', False, TABS.epoch)
        assert out == '- textbox "Email": a@b.example'


class TestHmacTokens:
    @pytest.mark.asyncio
    async def test_values_are_never_kept_only_keyed_hashes(self):
        page = _page()
        await payment_tab.arm(page)
        payment_tab.begin_fill(page, FILLED)
        payment_tab.mark_filled(page)
        entry = TABS._tabs[page]
        dump = repr(vars(entry)) + repr(vars(TABS))
        for v in FILLED + ("4242", "1234", "Tester"):
            assert v not in dump
            assert v.encode() not in b"".join(entry.digests)
        assert len(entry.key) == 32

    @pytest.mark.asyncio
    async def test_each_arming_has_its_own_key(self):
        a, b = _page(), _page()
        await payment_tab.arm(a)
        key_a = TABS._tabs[a].key
        payment_tab.disarm(a)
        await payment_tab.arm(b)
        assert TABS._tabs[b].key != key_a

    @pytest.mark.asyncio
    async def test_matching_tokens_are_hidden_on_any_page_others_kept(self):
        await _arm_in("armed", _page())
        text = (
            f"card 4242 4242 4242 4242 exp {EXP} or 1234 cvc {CVC} by Tester; total 740 Al."
            f" Thanks, Pat Tester. https://x.example/done?cvc={CVC}&exp=1234&n=Pat+Tester"
            f" /done?n=Pat%20Tester"
        )
        out = TABS.redact_text(text)
        _no_secret(out)
        assert "total 740 Al." in out  # short or non-matching tokens stay

    @pytest.mark.asyncio
    async def test_values_are_matched_from_begin_fill_before_any_is_typed(self):
        # A fill that fails after typing some fields never reaches mark_filled:
        # text mirroring what was typed must already be hidden.
        page = _page()
        await payment_tab.arm(page)
        payment_tab.begin_fill(page, FILLED)
        assert TABS.state(page) is payment_tab.State.FILLING
        out = TABS.redact_text(f"Paying with card {CVC} of {NAME}, exp {EXP}")
        _no_secret(out)
        _drive(page)
        _no_secret(await browser._impl_browser_snapshot())

    @pytest.mark.asyncio
    async def test_mark_filled_needs_begin_fill_first(self):
        # begin_fill is where the mask and the pending file are checked.
        page = _page()
        await payment_tab.arm(page)
        with pytest.raises(PaymentTabError) as e:
            payment_tab.mark_filled(page, FILLED)
        assert e.value.code == "wrong_state"
        assert TABS.state(page) is payment_tab.State.AWAITING_TAP

    @pytest.mark.asyncio
    async def test_tokens_stop_matching_once_the_tab_is_finished(self):
        page = _page()
        await _arm_in("armed", page)
        await payment_tab.finish(page)
        assert TABS.redact_text(f"cvc {CVC}") == f"cvc {CVC}"


# ---------------------------------------------------------------------------
# The taint
# ---------------------------------------------------------------------------


class TestTaint:
    @pytest.mark.asyncio
    async def test_run_js_on_camoufox_taints_and_arming_then_refuses(self):
        page = _page()
        _drive(page)
        assert (await browser._impl_browser_run_js("1+1"))["result"] == "ran"
        assert TABS.tainted()
        with pytest.raises(PaymentTabError) as e:
            await payment_tab.arm(_page())
        assert e.value.code == "browser_tainted_restart"

    @pytest.mark.asyncio
    async def test_the_taint_is_set_before_the_script_runs(self):
        page = _page()
        _drive(page)
        page.evaluate = AsyncMock(
            side_effect=lambda *_a, **_k: TABS.tainted() or pytest.fail("untainted")
        )
        await browser._impl_browser_run_js("1")

    @pytest.mark.asyncio
    async def test_run_js_on_chromium_does_not_taint_camoufox(self):
        page = _page()
        browser._active_page = browser._page = page
        await browser._impl_browser_run_js("1")
        assert not TABS.tainted()

    @pytest.mark.asyncio
    async def test_a_refused_run_js_runs_nothing_and_taints_nothing(self):
        page = _page()
        _drive(page)
        await browser._impl_browser_run_js(f"'{PAN}'")
        assert not TABS.tainted()
        page.evaluate.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "url",
        ["javascript:alert(1)", "data:text/html,x", "about:blank", "file:///etc/hostname", ""],
    )
    async def test_a_non_http_navigation_taints(self, url):
        page = _page()
        with (
            patch.object(browser, "_get_page", AsyncMock(return_value=(page, False))),
            patch.object(browser, "_wait_for_turnstile", AsyncMock(return_value=None)),
        ):
            await browser._impl_browser_navigate(url)
        assert TABS.tainted()

    @pytest.mark.asyncio
    async def test_an_http_navigation_does_not_taint(self):
        page = _page()
        with (
            patch.object(browser, "_get_page", AsyncMock(return_value=(page, False))),
            patch.object(browser, "_wait_for_turnstile", AsyncMock(return_value=None)),
        ):
            await browser._impl_browser_navigate("https://shop.example/")
        assert not TABS.tainted()

    @pytest.mark.asyncio
    async def test_only_a_camoufox_restart_clears_it(self):
        TABS.taint("test")
        TABS.forget_layer(BrowserLayer.CAMOUFOX)
        assert TABS.tainted()
        await browser._cleanup_camoufox()
        assert not TABS.tainted()


# ---------------------------------------------------------------------------
# The reader matrix
# ---------------------------------------------------------------------------


class TestReaderMatrix:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", STATES)
    async def test_run_js_is_refused_on_any_tab(self, state):
        armed, other = _page(), _page("https://other.example/")
        await _arm_in(state, armed)
        for page in (armed, other):
            _drive(page)
            result = await browser._impl_browser_run_js("document.title")
            assert result == {"error": browser._REFUSE_PAYMENT_RUN_JS}
            page.evaluate.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", STATES)
    async def test_screenshot_fill_upload_are_refused_on_the_armed_tab(self, state, tmp_path):
        page = _page()
        await _arm_in(state, page)
        _drive(page)
        f = tmp_path / "doc.txt"
        f.write_text("x")
        for result in (
            await browser._impl_browser_screenshot(),
            await browser._impl_browser_fill("#email", "a@b.example"),
            await browser._impl_browser_upload("#file", str(f)),
        ):
            assert result == {"error": browser._REFUSE_PAYMENT_TAB}
        page.screenshot.assert_not_awaited()
        page.set_input_files.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_screenshot_of_another_tab_is_allowed(self):
        await _arm_in("armed", _page())
        other = _page("https://other.example/")
        _drive(other)
        result = await browser._impl_browser_screenshot()
        assert "path" in result
        other.screenshot.assert_awaited_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", STATES)
    async def test_navigate_on_the_armed_tab_is_refused(self, state):
        page = _page()
        await _arm_in(state, page)
        with patch.object(browser, "_get_page", AsyncMock(return_value=(page, False))):
            result = await browser._impl_browser_navigate("https://shop.example/next")
        assert result == {"error": browser._REFUSE_PAYMENT_TAB}
        page.goto.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", STATES)
    async def test_a_non_http_navigation_anywhere_is_refused(self, state):
        await _arm_in(state, _page())
        get_page = AsyncMock()
        with patch.object(browser, "_get_page", get_page):
            result = await browser._impl_browser_navigate(
                "javascript:document.forms[0]", stealth=False
            )
        assert result == {"error": browser._REFUSE_PAYMENT_SCHEME}
        get_page.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", STATES)
    async def test_press_key_takes_only_the_allowlist_on_the_armed_tab(self, state):
        page = _page()
        await _arm_in(state, page)
        _drive(page)
        edits = ("Space", " ", "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight")
        edits += ("PageUp", "PageDown", "Home", "End")  # each changes a <select> (MEASURED)
        for key in (
            "Control+c",
            "Control+a",
            "Backspace",
            "Shift+ArrowLeft",
            "Control+v",
            "a",
            *edits,
        ):
            assert await browser._impl_browser_press_key(key) == {
                "error": browser._REFUSE_PAYMENT_KEY
            }
        page.keyboard.press.assert_not_awaited()
        for key in sorted(payment_tab.ALLOWED_KEYS):
            assert "pressed" in await browser._impl_browser_press_key(key)

    def test_the_allowlist_holds_no_key_that_edits_a_card_control(self):
        assert {"Enter", "Tab", "Shift+Tab", "Escape"} == payment_tab.ALLOWED_KEYS

    @pytest.mark.asyncio
    async def test_press_key_on_another_tab_is_not_limited(self):
        await _arm_in("armed", _page())
        other = _page("https://other.example/")
        _drive(other)
        assert "pressed" in await browser._impl_browser_press_key("Control+a")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", ("awaiting_tap", "filling", "armed"))
    async def test_snapshot_output_is_redacted(self, state):
        page = _page(url=f"https://u:p@shop.example/pay?cc={PAN}&cvc={CVC}#x", title=f"Pay {CVC}")
        await _arm_in(state, page)
        _drive(page)
        result = await browser._impl_browser_snapshot()
        assert result["url"] == "https://shop.example/pay"
        assert f'- textbox "Card number": {HIDDEN}' in result["snapshot"]
        if state == "armed":  # page text matches filled values only once filled
            _no_secret(result)

    @pytest.mark.asyncio
    async def test_a_crashed_payment_tab_snapshots_as_a_fixed_text(self):
        page = _page()
        await _arm_in("crashed", page)
        _drive(page)
        result = await browser._impl_browser_snapshot()
        assert result["snapshot"] == "(snapshot unavailable: the payment tab crashed)"
        page.locator.return_value.aria_snapshot.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_tab_finished_during_the_read_shows_nothing(self):
        page = _page()
        page.context.new_page = AsyncMock(return_value=_page("about:blank"))
        await _arm_in("armed", page)

        async def read():
            await payment_tab.finish(page)  # lands while the snapshot is in flight
            return SNAPSHOT

        page.locator.return_value.aria_snapshot = AsyncMock(side_effect=read)
        assert (
            await browser._snapshot_page(page) == "(snapshot unavailable: the payment tab closed)"
        )

    @pytest.mark.asyncio
    async def test_the_snapshot_url_stays_path_only_when_finish_lands_in_the_read(self):
        page = _page(url=f"https://shop.example/pay?cvc={CVC}&n=Pat+Tester")
        page.context.new_page = AsyncMock(return_value=_page("about:blank"))
        await _arm_in("armed", page)
        _drive(page)

        async def read():
            await payment_tab.finish(page)
            return SNAPSHOT

        page.locator.return_value.aria_snapshot = AsyncMock(side_effect=read)
        browser._active_page = page  # finish moved the tools; this call already holds the page
        result = await browser._impl_browser_snapshot()
        assert result["url"] == "https://shop.example/pay"
        _no_secret(result)

    @pytest.mark.asyncio
    async def test_a_snapshot_error_on_the_armed_tab_is_fixed_text(self):
        page = _page()
        await _arm_in("armed", page)
        _drive(page)
        page.locator.return_value.aria_snapshot = AsyncMock(
            side_effect=RuntimeError(f"<input value={CVC}>")
        )
        result = await browser._impl_browser_snapshot()
        assert result["snapshot"] == "(snapshot unavailable: payment tab, detail withheld)"

    @pytest.mark.asyncio
    async def test_an_error_after_the_tab_was_finished_mid_call_is_still_fixed_text(self):
        # Armed when the call began, disarmed (finished) before its error:
        # the error is judged by the arming the call started under.
        for tool in ("snapshot", "press_key"):
            TABS.reset()
            TABS.prefs_pinned = True
            page = _page()
            await _arm_in("armed", page)
            _drive(page)

            def finished_then_fail(*_a, _page=page, **_kw):
                TABS._tabs.pop(_page, None)
                raise RuntimeError(f"<input value={CVC}>")

            if tool == "snapshot":
                page.title = AsyncMock(side_effect=finished_then_fail)
                result = await browser._impl_browser_snapshot()
            else:
                page.keyboard.press = AsyncMock(side_effect=finished_then_fail)
                result = await browser._impl_browser_press_key("Enter")
            assert result == {"error": browser._PAYMENT_TAB_FAILED}, tool

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", STATES)
    async def test_ts_log_writes_nothing(self, state):
        await _arm_in(state, _page())
        browser._ts_log.info("goto %s", f"https://shop.example/?cvc={CVC}")
        assert not browser._TS_LOG_PATH.exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", STATES)
    async def test_card_filter_withholds_args_and_tracebacks(self, state):
        await _arm_in(state, _page())
        try:
            raise ValueError(f"value {CVC}")
        except ValueError:
            record = logging.LogRecord(
                "x", logging.ERROR, __file__, 1, "fill %s", (EXP,), sys.exc_info()
            )
        CARD_FILTER.filter(record)
        text = logging.Formatter().format(record)
        _no_secret(text)
        assert "args withheld" in text and "ValueError" not in text

    @pytest.mark.asyncio
    async def test_a_second_payment_tab_is_refused(self):
        await _arm_in("armed", _page())
        with pytest.raises(PaymentTabError) as e:
            await payment_tab.arm(_page())
        assert e.value.code == "other_payment_tab"

    @pytest.mark.asyncio
    async def test_finish_with_no_payment_tab(self):
        with pytest.raises(PaymentTabError) as e:
            await payment_tab.finish(_page())
        assert e.value.code == "no_payment_tab"

    @pytest.mark.asyncio
    async def test_a_filled_tab_cannot_be_disarmed_only_finished(self):
        page = _page()
        await _arm_in("armed", page)
        with pytest.raises(PaymentTabError) as e:
            payment_tab.disarm(page)
        assert e.value.code == "wrong_state"

    @pytest.mark.asyncio
    async def test_guards_follow_the_page_when_it_is_not_active(self):
        """A popup made active: the armed tab stays armed, run_js stays refused,
        and the guards apply again once the tools are back on it."""
        armed, popup = _page(), _page("https://bank.example/3ds")
        await _arm_in("armed", armed)
        _drive(popup)
        assert "path" in await browser._impl_browser_screenshot()
        assert (await browser._impl_browser_run_js("1"))["error"] == browser._REFUSE_PAYMENT_RUN_JS
        _drive(armed)
        assert await browser._impl_browser_screenshot() == {"error": browser._REFUSE_PAYMENT_TAB}

    @pytest.mark.asyncio
    async def test_closed_column_everything_back_to_normal(self, tmp_path):
        page = _page()
        await _arm_in("armed", page)
        await page.close()  # closed by the site or the user, not by finish
        await asyncio.sleep(0)
        await asyncio.gather(*TABS._tasks)
        assert not TABS.any_armed() and not TABS.run_js_blocked()
        _drive(page)
        assert (await browser._impl_browser_run_js("1"))["result"] == "ran"
        browser._ts_log.info("written")
        assert "written" in browser._TS_LOG_PATH.read_text()
        with (
            patch.object(browser, "_get_page", AsyncMock(return_value=(page, False))),
            patch.object(browser, "_wait_for_turnstile", AsyncMock(return_value=None)),
        ):
            await browser._impl_browser_navigate("about:blank")
        assert TABS.tainted()  # a non-http navigation taints again

    @pytest.mark.asyncio
    async def test_layer_reclaimed_column(self):
        page = _page()
        await _arm_in("armed", page)
        _drive(page)
        await browser._cleanup_camoufox()
        assert not TABS.any_armed() and not TABS.tainted()
        # The profile outlives the browser: the tab's storage is cleared at the
        # next launch, and run_js waits for it.
        assert TABS.run_js_blocked()
        await TABS.clear_pending(MagicMock(), BrowserLayer.CAMOUFOX)
        payment_tab.clear_site_storage.assert_awaited_with(ANY, {"https://shop.example"})
        assert not TABS.run_js_blocked()

    @pytest.mark.asyncio
    async def test_another_layer_reclaimed_keeps_the_arming(self):
        await _arm_in("armed", _page())
        await browser._cleanup_chromium()
        assert TABS.any_armed()

    @pytest.mark.asyncio
    async def test_async_cleanup_clears_everything(self):
        await _arm_in("armed", _page())
        TABS.taint("x")
        await browser.async_cleanup()
        assert not TABS.any_armed() and not TABS.tainted()
        assert TABS.move_off is browser._move_off_payment_tab  # the hook survives


@pytest.mark.asyncio
async def test_mcp_exit_lets_a_running_storage_clear_finish_before_closing_camoufox(monkeypatch):
    order = []

    async def slow_clear(context, origins):
        await asyncio.sleep(0.05)
        order.append("cleared")
        return True

    async def close_camoufox():
        order.append("camoufox closed")

    monkeypatch.setattr(payment_tab, "clear_site_storage", slow_clear)
    monkeypatch.setattr(browser, "_cleanup_camoufox", close_camoufox)
    page = _page()
    await _arm_in("armed", page)
    await page.close()  # the clear starts in the background
    await browser.async_cleanup()
    assert order == ["cleared", "camoufox closed"]


@pytest.mark.asyncio
async def test_mcp_exit_finishes_an_armed_tab_before_closing_camoufox(monkeypatch):
    order = []

    async def clear(context, origins):
        order.append(("cleared", frozenset(origins)))
        return True

    async def close_camoufox():
        order.append("camoufox closed")

    monkeypatch.setattr(payment_tab, "clear_site_storage", clear)
    monkeypatch.setattr(browser, "_cleanup_camoufox", close_camoufox)
    page = _page()
    page.context.new_page = AsyncMock(return_value=_page("about:blank"))
    _drive(page)
    await _arm_in("armed", page)
    await browser.async_cleanup()
    assert order == [("cleared", frozenset({"https://shop.example"})), "camoufox closed"]
    page.close.assert_awaited_once_with(run_before_unload=False)
    assert not TABS.run_js_blocked()
    assert not env.payment_tab_pending_path().exists()


@pytest.mark.asyncio
async def test_mcp_exit_keeps_a_tab_that_would_not_close_pending_on_disk(monkeypatch):
    monkeypatch.setattr(browser, "_cleanup_camoufox", AsyncMock())
    page = _page()
    page.context.new_page = AsyncMock(return_value=_page("about:blank"))
    page.close = AsyncMock(side_effect=RuntimeError("busy"))
    await _arm_in("armed", page)
    await browser.async_cleanup()
    page.close.assert_awaited()  # the exit tried to finish it
    assert json.loads(env.payment_tab_pending_path().read_text()) == ["https://shop.example"]


# The real _CLEAR_STORAGE_JS, run by node against a fake IndexedDB whose
# deleteDatabase request ends with the given event.
_FAKE_STORAGE = """
const outcome = process.argv[1];
globalThis.self = globalThis;
globalThis.localStorage = { clear() {} };
globalThis.sessionStorage = { clear() {} };
globalThis.indexedDB = {
  deleteDatabase() {
    const q = {};
    setTimeout(() => {
      // blocked is a notice: by spec the same request still ends in success
      // once the other connections close (or stays blocked).
      if (outcome.startsWith('blocked')) q.onblocked && q.onblocked();
      if (outcome === 'blocked_then_success') setTimeout(() => q.onsuccess(), 50);
      else if (outcome !== 'blocked') q['on' + outcome]();
    }, 0);
    return q;
  },
};
if (outcome !== 'unenumerable') indexedDB.databases = async () => [{ name: 'cart' }];
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize(
    ("outcome", "ok"),
    [
        ("success", True),
        ("error", False),
        ("blocked", False),  # still blocked when the bounded wait ends
        ("blocked_then_success", True),
        ("unenumerable", False),
    ],
)
def test_the_storage_clear_counts_only_a_deleted_database(outcome, ok):
    script = (
        _FAKE_STORAGE
        + "("
        + payment_tab._CLEAR_STORAGE_JS
        + ")().then((v) => console.log(JSON.stringify(v)));"
    )
    out = subprocess.run(
        ["node", "-e", script, outcome], capture_output=True, text=True, timeout=60, check=True
    )
    assert json.loads(out.stdout) is ok


class TestPendingFile:
    """Origins a payment tab showed are on disk from arm until their storage
    is cleared, so an MCP crash or kill leaves them for the next launch."""

    @pytest.mark.asyncio
    async def test_an_armed_tab_s_origins_are_on_disk_from_arm(self):
        page = _page()
        await payment_tab.arm(page)
        path = env.payment_tab_pending_path()
        assert json.loads(path.read_text()) == ["https://shop.example"]
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        for fn in page.handlers["framenavigated"]:
            fn(MagicMock(url="https://pay.hosted.example/s/1"))
        assert json.loads(path.read_text()) == [
            "https://pay.hosted.example",
            "https://shop.example",
        ]

    @pytest.mark.asyncio
    async def test_a_killed_process_s_origins_are_cleared_at_the_next_launch(self, monkeypatch):
        await _arm_in("armed", _page())
        TABS.reset()  # the MCP process was killed: its memory is gone, the file is not
        path = env.payment_tab_pending_path()
        clear = AsyncMock(return_value=False)
        monkeypatch.setattr(payment_tab, "clear_site_storage", clear)
        await TABS.clear_pending(MagicMock(), BrowserLayer.CAMOUFOX)
        clear.assert_awaited_once_with(ANY, {"https://shop.example"})
        # A failed clear keeps the entry, and run_js stays refused.
        assert TABS.run_js_blocked() and path.exists()
        _drive(_page("https://other.example/"))
        assert await browser._impl_browser_run_js("1") == {"error": browser._REFUSE_PAYMENT_RUN_JS}
        clear.return_value = True
        await TABS.clear_pending(MagicMock(), BrowserLayer.CAMOUFOX)
        assert not TABS.run_js_blocked() and not path.exists()

    @pytest.mark.asyncio
    async def test_finish_drops_an_origin_from_disk_only_after_its_clear(self, monkeypatch):
        gate = asyncio.Event()

        async def slow_clear(context, origins):
            await gate.wait()
            return True

        monkeypatch.setattr(payment_tab, "clear_site_storage", slow_clear)
        page = _page()
        page.context.new_page = AsyncMock(return_value=_page("about:blank"))
        await _arm_in("armed", page)
        done = asyncio.ensure_future(payment_tab.finish(page))
        await asyncio.sleep(0.01)
        path = env.payment_tab_pending_path()
        assert json.loads(path.read_text()) == ["https://shop.example"]
        gate.set()
        await done
        assert not path.exists()

    @pytest.mark.asyncio
    async def test_an_unreadable_file_keeps_run_js_refused_and_refuses_arming(self):
        path = env.payment_tab_pending_path()
        path.write_text("{not json")
        await TABS.clear_pending(MagicMock(), BrowserLayer.CAMOUFOX)
        assert TABS.run_js_blocked()
        with pytest.raises(PaymentTabError) as e:
            await payment_tab.arm(_page())
        assert e.value.code == "pending_store_failed"
        assert path.read_text() == "{not json"  # left for the owner to inspect

    @pytest.mark.asyncio
    async def test_arming_refuses_when_the_file_cannot_be_written(self, monkeypatch, tmp_path):
        (tmp_path / "not-a-dir").write_text("")
        monkeypatch.setattr(
            env, "payment_tab_pending_path", lambda: tmp_path / "not-a-dir" / "pending.json"
        )
        page = _page()
        with pytest.raises(PaymentTabError) as e:
            await payment_tab.arm(page)
        assert e.value.code == "pending_store_failed"
        assert not TABS.is_armed(page)

    @pytest.mark.asyncio
    async def test_another_process_s_origins_are_kept(self):
        path = env.payment_tab_pending_path()
        path.write_text(json.dumps(["https://bank.example"]))  # another MCP process's
        page = _page()
        page.context.new_page = AsyncMock(return_value=_page("about:blank"))
        await _arm_in("armed", page)
        assert json.loads(path.read_text()) == ["https://bank.example", "https://shop.example"]
        await payment_tab.finish(page)
        assert json.loads(path.read_text()) == ["https://bank.example"]

    @pytest.mark.asyncio
    async def test_an_unreadable_file_says_so_and_recovers_once_removed(self):
        path = env.payment_tab_pending_path()
        path.write_text("{not json")
        await TABS.clear_pending(MagicMock(), BrowserLayer.CAMOUFOX)
        _drive(_page("https://other.example/"))
        assert await browser._impl_browser_run_js("1") == {"error": browser._REFUSE_PAYMENT_STORE}
        path.unlink()  # the owner checked it and removed it
        await TABS.clear_pending(MagicMock(), BrowserLayer.CAMOUFOX)
        assert not TABS.run_js_blocked()

    @pytest.mark.asyncio
    async def test_a_stray_temp_file_is_removed_at_launch(self):
        path = env.payment_tab_pending_path()
        stray = path.parent / f".{path.name}.abc123"
        stray.write_text('["https://shop.example"]')  # a kill between write and rename
        await TABS.clear_pending(MagicMock(), BrowserLayer.CAMOUFOX)
        assert not stray.exists()

    @pytest.mark.asyncio
    async def test_the_launch_pins_prefs_only_after_the_pending_clear(self, monkeypatch, tmp_path):
        seen = []

        async def clear(context, layer):
            seen.append(TABS.prefs_pinned)

        monkeypatch.setattr(TABS, "clear_pending", clear)
        TABS.prefs_pinned = False  # nothing launched yet in this process
        await TestLaunchPrefs()._launch(monkeypatch, tmp_path, True)
        assert seen == [False] and TABS.prefs_pinned is True

    def test_the_path_is_under_genesis_home(self, monkeypatch, tmp_path):
        monkeypatch.setenv("GENESIS_HOME", str(tmp_path))
        assert _REAL_PENDING_PATH() == tmp_path / "payment_tab_pending.json"


class TestClickOnArmedTab:
    @pytest.mark.asyncio
    async def test_urls_are_path_only_and_titles_token_redacted(self, monkeypatch):
        page = _page(url=f"https://shop.example/pay?cvc={CVC}")
        popup = _page(url=f"https://bank.example/3ds?pan={PAN}", title=f"Verify {CVC}")
        extra = _page(url=f"https://ads.example/x?exp={EXP}")
        browser._stealth_cm = MagicMock()
        _drive(page)
        await _arm_in("armed", page)
        monkeypatch.setattr(browser, "_declares_new_tab", AsyncMock(return_value=False))

        async def click(p, selector):
            for fn in p.handlers.get("popup", []):
                fn(popup)
                fn(extra)

        monkeypatch.setattr(browser, "_stealth_click", click)
        result = await browser._impl_browser_click("#pay")
        _no_secret(result)
        assert result["new_page"]["url"] == "https://bank.example/3ds"
        assert result["new_page"]["also_opened"] == ["https://ads.example/x"]
        assert result["new_page"]["title"] == f"Verify {HIDDEN}"

    @pytest.mark.asyncio
    async def test_errors_are_fixed_text(self, monkeypatch):
        page = _page()
        _drive(page)
        await _arm_in("armed", page)
        monkeypatch.setattr(browser, "_declares_new_tab", AsyncMock(return_value=False))
        monkeypatch.setattr(
            browser,
            "_stealth_click",
            AsyncMock(side_effect=RuntimeError(f'<input value="{PAN}" name=cvc {CVC}>')),
        )
        assert await browser._impl_browser_click("#pay") == {"error": browser._PAYMENT_TAB_FAILED}

    @pytest.mark.asyncio
    async def test_the_click_url_on_a_normal_tab_is_unchanged(self, monkeypatch):
        page = _page(url="https://shop.example/list?page=2")
        _drive(page)
        monkeypatch.setattr(browser, "_declares_new_tab", AsyncMock(return_value=False))
        monkeypatch.setattr(browser, "_stealth_click", AsyncMock())
        assert (await browser._impl_browser_click("#next"))[
            "url"
        ] == "https://shop.example/list?page=2"


# ---------------------------------------------------------------------------
# finish, storage, closing
# ---------------------------------------------------------------------------


class TestFinish:
    @pytest.mark.asyncio
    async def test_finish_moves_the_tools_to_a_fresh_page_then_closes(self):
        page, fresh = _page(), _page("about:blank")
        page.context.new_page = AsyncMock(return_value=fresh)
        _drive(page)
        await _arm_in("armed", page)
        result = await payment_tab.finish(page)
        assert result == {"closed": True, "storage_cleared": True}
        assert browser._active_page is fresh and browser._stealth_page is fresh
        page.close.assert_awaited_once_with(run_before_unload=False)
        payment_tab.clear_site_storage.assert_awaited_once_with(
            page.context, {"https://shop.example"}
        )
        assert not TABS.any_armed() and not TABS.run_js_blocked()

    @pytest.mark.asyncio
    async def test_an_undriven_tab_with_siblings_opens_no_page(self):
        page, other = _page(), _page("https://other.example/")
        page.context.pages = [other, page]
        page.context.new_page = AsyncMock()
        _drive(other)
        await _arm_in("armed", page)
        await payment_tab.finish(page)
        page.context.new_page.assert_not_awaited()
        assert browser._active_page is other

    @pytest.mark.asyncio
    async def test_run_js_stays_refused_until_the_storage_is_cleared(self, monkeypatch):
        gate = asyncio.Event()

        async def slow_clear(context, origins):
            await gate.wait()
            return True

        monkeypatch.setattr(payment_tab, "clear_site_storage", slow_clear)
        page = _page()
        page.context.new_page = AsyncMock(return_value=_page("about:blank"))
        await _arm_in("armed", page)
        done = asyncio.ensure_future(payment_tab.finish(page))
        await asyncio.sleep(0.01)
        assert not TABS.any_armed() and TABS.run_js_blocked()
        gate.set()
        assert (await done)["storage_cleared"] is True
        assert not TABS.run_js_blocked()

    @pytest.mark.asyncio
    async def test_a_failed_clear_is_retried_at_the_next_launch(self, monkeypatch):
        monkeypatch.setattr(payment_tab, "clear_site_storage", AsyncMock(return_value=False))
        page = _page()
        page.context.new_page = AsyncMock(return_value=_page("about:blank"))
        await _arm_in("armed", page)
        assert (await payment_tab.finish(page))["storage_cleared"] is False
        assert TABS.run_js_blocked()
        await browser._cleanup_camoufox()
        assert TABS.run_js_blocked()  # a restart alone does not clear the profile
        monkeypatch.setattr(payment_tab, "clear_site_storage", AsyncMock(return_value=True))
        await TABS.clear_pending(MagicMock(), BrowserLayer.CAMOUFOX)
        assert not TABS.run_js_blocked()

    @pytest.mark.asyncio
    async def test_the_launch_clears_what_the_last_launch_left(self, monkeypatch):
        clear = AsyncMock()
        monkeypatch.setattr(TABS, "clear_pending", clear)
        await TestLaunchPrefs()._launch(
            monkeypatch, Path(browser.__file__).parent / "nonexistent", False
        )
        clear.assert_awaited_once_with(ANY, BrowserLayer.CAMOUFOX)

    @pytest.mark.asyncio
    async def test_a_second_finish_while_one_runs_is_refused(self, monkeypatch):
        gate = asyncio.Event()

        async def slow_move(_p):
            await gate.wait()

        monkeypatch.setattr(TABS, "move_off", slow_move)
        page = _page()
        await _arm_in("armed", page)
        first = asyncio.ensure_future(payment_tab.finish(page))
        await asyncio.sleep(0)
        with pytest.raises(PaymentTabError) as e:
            await payment_tab.finish(page)
        assert e.value.code == "wrong_state"
        gate.set()
        assert (await first)["closed"] is True

    @pytest.mark.asyncio
    async def test_an_origin_the_tab_passed_through_is_cleared(self):
        page = _page()
        page.context.new_page = AsyncMock(return_value=_page("about:blank"))
        await _arm_in("armed", page)
        for fn in page.handlers["framenavigated"]:
            fn(MagicMock(url="https://pay.hosted.example/session/1"))
        await payment_tab.finish(page)
        origins = payment_tab.clear_site_storage.await_args.args[1]
        parsed = {(urlsplit(o).scheme, urlsplit(o).hostname) for o in origins}
        assert ("https", "pay.hosted.example") in parsed

    @pytest.mark.asyncio
    async def test_re_arming_registers_the_handlers_once(self):
        page = _page()
        await payment_tab.arm(page)
        payment_tab.disarm(page)
        await payment_tab.arm(page)
        assert len(page.handlers["close"]) == 1 and len(page.handlers["load"]) == 1

    @pytest.mark.asyncio
    async def test_a_tab_that_will_not_close_stays_armed(self):
        page = _page()
        page.context.new_page = AsyncMock(return_value=_page("about:blank"))
        page.close = AsyncMock(side_effect=RuntimeError("busy"))
        await _arm_in("armed", page)
        assert await payment_tab.finish(page) == {"closed": False, "storage_cleared": False}
        assert TABS.is_armed(page)
        assert TABS.state(page) is payment_tab.State.ARMED  # not left CLOSING
        page.close = AsyncMock(
            side_effect=lambda **_kw: page.is_closed.configure_mock(return_value=True)
        )
        assert (await payment_tab.finish(page))["closed"] is True  # a retry can finish it

    @pytest.mark.asyncio
    async def test_frame_origins_are_cleared_too(self):
        page = _page()
        frame = MagicMock(url="https://js.stripe.example/v3/elements")
        frame.evaluate = AsyncMock(return_value=True)
        page.frames.append(frame)
        page.context.new_page = AsyncMock(return_value=_page("about:blank"))
        await _arm_in("armed", page)
        await payment_tab.finish(page)
        args = payment_tab.clear_site_storage.await_args.args
        assert args[1] == {"https://shop.example", "https://js.stripe.example"}

    @pytest.mark.asyncio
    async def test_arming_masks_every_frame_and_on_each_new_document(self):
        page = _page()
        assert await payment_tab.arm(page) is True
        page.add_init_script.assert_awaited_once_with(payment_tab._MASK_INIT_JS)
        page.frames[0].evaluate.assert_any_await(payment_tab._MASK_JS)
        page.frames[0].evaluate.assert_any_await(payment_tab._MASK_CHECK_JS)
        page.frames[0].evaluate.reset_mock()
        _fire(page, "load")
        await asyncio.gather(*TABS._tasks)
        page.frames[0].evaluate.assert_awaited_once_with(payment_tab._MASK_JS)
        frame = MagicMock(evaluate=AsyncMock())
        for fn in page.handlers["framenavigated"]:
            fn(frame)
        await asyncio.gather(*TABS._tasks)
        frame.evaluate.assert_awaited_once_with(payment_tab._MASK_JS)

    @pytest.mark.asyncio
    async def test_a_failed_init_script_fails_the_arm(self):
        page = _page()
        page.add_init_script = AsyncMock(side_effect=RuntimeError("closed"))
        with pytest.raises(PaymentTabError) as e:
            await payment_tab.arm(page)
        assert e.value.code == "mask_failed"
        assert not TABS.is_armed(page)  # nothing was filled: the tab is released
        assert not env.payment_tab_pending_path().exists()

    @pytest.mark.asyncio
    async def test_arming_refuses_a_browser_launched_without_the_card_prefs(self):
        TABS.prefs_pinned = False  # launched while card_fill.enabled was false
        page = _page()
        with pytest.raises(PaymentTabError) as e:
            await payment_tab.arm(page)
        assert e.value.code == "prefs_not_pinned"
        assert not TABS.is_armed(page)
        page.add_init_script.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_arming_refuses_a_layer_with_no_card_prefs(self):
        with pytest.raises(PaymentTabError) as e:
            await payment_tab.arm(_page(), BrowserLayer.CHROMIUM)
        assert e.value.code == "prefs_not_pinned"

    @pytest.mark.asyncio
    async def test_a_mask_that_does_not_verify_is_reported(self):
        page = _page()
        page.frames[0].evaluate = AsyncMock(side_effect=lambda js: js != payment_tab._MASK_CHECK_JS)
        assert await payment_tab.arm(page) is False
        assert TABS.is_armed(page)  # still armed: the guards hold whatever the mask did
        with pytest.raises(PaymentTabError) as e:
            payment_tab.begin_fill(page, FILLED)  # but nothing is filled into it
        assert e.value.code == "mask_unverified"
        page.frames[0].evaluate = AsyncMock(return_value=True)
        assert await TABS.verify_mask(page) is True
        payment_tab.begin_fill(page, FILLED)

    @pytest.mark.asyncio
    async def test_a_navigation_after_arming_needs_a_new_mask_check(self):
        page = _page()
        assert await payment_tab.arm(page) is True
        for fn in page.handlers["framenavigated"]:
            fn(MagicMock(url="https://pay.hosted.example/s/1", evaluate=AsyncMock()))
        with pytest.raises(PaymentTabError) as e:
            payment_tab.begin_fill(page, FILLED)
        assert e.value.code == "mask_unverified"
        assert await TABS.verify_mask(page) is True
        payment_tab.begin_fill(page, FILLED)

    @pytest.mark.asyncio
    async def test_a_cancelled_finish_leaves_the_tab_armed_and_finishable(self):
        page = _page()
        page.context.new_page = AsyncMock(return_value=_page("about:blank"))
        await _arm_in("armed", page)
        page.close = AsyncMock(side_effect=asyncio.CancelledError)
        with pytest.raises(asyncio.CancelledError):
            await payment_tab.finish(page)
        assert TABS.state(page) is payment_tab.State.ARMED

    @pytest.mark.asyncio
    async def test_begin_fill_refuses_when_an_origin_is_not_on_disk(self, monkeypatch, tmp_path):
        page = _page()
        await payment_tab.arm(page)
        (tmp_path / "not-a-dir").write_text("")
        monkeypatch.setattr(
            env, "payment_tab_pending_path", lambda: tmp_path / "not-a-dir" / "pending.json"
        )
        page.frames[0].url = "https://pay.hosted.example/s/1"  # a redirect not yet on disk
        with pytest.raises(PaymentTabError) as e:
            payment_tab.begin_fill(page, FILLED)
        assert e.value.code == "pending_store_failed"
        assert TABS.state(page) is payment_tab.State.AWAITING_TAP

    def test_mask_rule_covers_every_glyph_source(self):
        for prop in (
            "color: transparent",
            "-webkit-text-fill-color: transparent",
            "caret-color: transparent",
            "text-shadow: none",
            "::selection",
        ):
            assert prop in payment_tab.MASK_CSS
        assert payment_tab.MASK_CSS in payment_tab._MASK_JS
        assert payment_tab._MASK_JS in payment_tab._MASK_INIT_JS


# ---------------------------------------------------------------------------
# Prefs and the config flag
# ---------------------------------------------------------------------------


class TestCardFillFlag:
    @pytest.fixture
    def repo(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GENESIS_REPO_ROOT", str(tmp_path))
        monkeypatch.delenv("GENESIS_CARD_FILL_DISABLED", raising=False)
        from genesis import _config_overlay

        monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: tmp_path / "user")
        (tmp_path / "config").mkdir()
        return tmp_path / "config" / "payments.yaml"

    @pytest.mark.parametrize(
        ("text", "on"),
        [
            (None, False),
            ("card_fill:\n  enabled: false\n", False),
            ("card_fill:\n  enabled: true\n", True),
            ("card_fill:\n  enabled: 'true'\n", False),
            ("card_fill: [1\n", False),
            ("- a\n", False),
        ],
    )
    def test_reader(self, repo, text, on):
        if text is not None:
            repo.write_text(text)
        assert payment_tab.card_fill_enabled() is on

    def test_kill_switch_wins(self, repo, monkeypatch):
        repo.write_text("card_fill:\n  enabled: true\n")
        monkeypatch.setenv("GENESIS_CARD_FILL_DISABLED", "1")
        assert payment_tab.card_fill_enabled() is False

    def test_the_overlay_turns_it_on(self, repo, tmp_path):
        repo.write_text("card_fill:\n  enabled: false\n")
        (tmp_path / "user").mkdir()
        (tmp_path / "user" / "payments.local.yaml").write_text("card_fill:\n  enabled: true\n")
        assert payment_tab.card_fill_enabled() is True


class TestLaunchPrefs:
    async def _launch(self, monkeypatch, tmp_path, enabled: bool) -> dict:
        from genesis.browser import engine, packages, profile_backup

        monkeypatch.setattr(payment_tab, "card_fill_enabled", lambda: enabled)
        monkeypatch.setattr(profile_backup, "backup_camoufox_profile", lambda *a: None)
        monkeypatch.setattr(packages, "check_loaded_browser_modules", lambda *a: None)
        monkeypatch.setattr(browser, "_PROFILE_DIR", tmp_path / "camoufox-profile")
        seen = {}
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=MagicMock(pages=[_page()]))

        def launch(**kw):
            seen.update(kw["firefox_user_prefs"])
            return cm

        api = MagicMock(AsyncCamoufox=MagicMock(side_effect=launch))
        ready = engine.EngineStatus(engine.READY, "Camoufox", Path("/engines/camoufox-bin"))
        with (
            patch.object(engine, "resolve_camoufox_engine", return_value=ready),
            patch.dict(
                sys.modules, {"camoufox": MagicMock(async_api=api), "camoufox.async_api": api}
            ),
        ):
            await browser._ensure_browser()
        return seen

    @pytest.mark.asyncio
    async def test_on_pins_all_eight(self, monkeypatch, tmp_path):
        prefs = await self._launch(monkeypatch, tmp_path, True)
        for k, v in payment_tab.CARD_FILL_PREFS.items():
            assert prefs[k] == v
        assert (
            prefs["browser.sessionstore.max_tabs_undo"] == 0
            and prefs["clipboard.autocopy"] is False
        )
        assert prefs["browser.sessionhistory.max_entries"] == 10
        assert TABS.prefs_pinned is True

    @pytest.mark.asyncio
    async def test_off_keeps_the_production_prefs(self, monkeypatch, tmp_path):
        prefs = await self._launch(monkeypatch, tmp_path, False)
        assert prefs == {
            "browser.sessionhistory.max_entries": 10,
            "browser.sessionhistory.max_total_viewers": -1,
        }
        assert TABS.prefs_pinned is False

    def test_eight_prefs(self):
        assert len(payment_tab.CARD_FILL_PREFS) == 8


# ---------------------------------------------------------------------------
# Contracts C2 and C3
# ---------------------------------------------------------------------------

_TREE = ast.parse(Path(browser.__file__).read_text(encoding="utf-8"))
_FUNCS = {n.name: n for n in _TREE.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)}

# C2: tools that need no arming check, each with its reason.
_ARMING_EXEMPT = {
    "_impl_browser_sessions": "reads cookie counts from the profile; reads and types into no page",
    "_impl_browser_clear_domain": "deletes cookies; reads and types into no page",
    "_impl_browser_collaborate": "switches the timing profile; touches no page",
}


def _uses_guard(fn) -> bool:
    return any(isinstance(n, ast.Name) and n.id == "payment_tab" for n in ast.walk(fn))


def test_c2_every_tool_checks_arming_or_is_named_exempt():
    tools = {name: fn for name, fn in _FUNCS.items() if name.startswith("_impl_browser_")}
    assert len(tools) >= 11, sorted(tools)

    def checks(fn) -> bool:
        if _uses_guard(fn):
            return True
        callees = {
            c.func.id
            for c in ast.walk(fn)
            if isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id in _FUNCS
        }
        return any(_uses_guard(_FUNCS[c]) for c in callees)

    unchecked = {name for name, fn in tools.items() if not checks(fn)}
    assert unchecked == set(_ARMING_EXEMPT), (
        f"check arming or add a reasoned exemption: {sorted(unchecked - set(_ARMING_EXEMPT))}; "
        f"stale exemptions: {sorted(set(_ARMING_EXEMPT) - unchecked)}"
    )
    assert all(r.strip() for r in _ARMING_EXEMPT.values())


_JS_CALLS = {
    "evaluate",
    "evaluate_handle",
    "evaluate_all",
    "add_init_script",
    "add_script_tag",
    "wait_for_function",
    "eval_on_selector",
    "eval_on_selector_all",
}


def _js_call_violations(path: Path, allowed_in: set[str]) -> tuple[list[str], int, int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    assigned: dict[str, list[ast.expr]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    assigned.setdefault(t.id, []).append(node.value)

    def const(e) -> bool:
        if isinstance(e, ast.Constant):
            return isinstance(e.value, str)
        if isinstance(e, ast.BinOp) and isinstance(e.op, ast.Add):
            return const(e.left) and const(e.right)
        if isinstance(e, ast.Name):
            vals = assigned.get(e.id, [])
            return len(vals) == 1 and const(vals[0])
        return False

    bad, checked, exempt = [], 0, 0
    for fn in ast.walk(tree):
        if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for c in ast.walk(fn):
            if not (
                isinstance(c, ast.Call)
                and isinstance(c.func, ast.Attribute)
                and c.func.attr in _JS_CALLS
            ):
                continue
            if fn.name in allowed_in:
                exempt += 1
                continue
            arg = (
                c.args[0]
                if c.args
                else next((k.value for k in c.keywords if k.arg in ("expression", "script")), None)
            )
            checked += 1
            if arg is None or not const(arg):
                bad.append(f"{path.name}:{c.lineno} in {fn.name}")
    return bad, checked, exempt


def test_c3_every_script_browser_py_runs_is_a_module_constant():
    bad, checked, exempt = _js_call_violations(Path(browser.__file__), {"_evaluate_main_world"})
    assert not bad, f"a page script built at run time: {bad}"
    assert checked >= 15 and exempt == 2, (checked, exempt)


def test_c3_holds_for_payment_tab_too():
    bad, checked, _ = _js_call_violations(Path(payment_tab.__file__), set())
    assert not bad and checked >= 5, (bad, checked)


def test_c3_catches_a_script_built_at_run_time(tmp_path):
    src = tmp_path / "m.py"
    src.write_text(
        'X = "1"\nasync def f(page, s):\n    await page.evaluate(X)\n    await page.evaluate("a" + s)\n'
    )
    bad, checked, _ = _js_call_violations(src, set())
    assert checked == 2 and len(bad) == 1


# ---------------------------------------------------------------------------
# No value in any result, error or log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_filled_value_reaches_any_tool_result_or_log(tmp_path, monkeypatch):
    """Every tool, run against the armed tab with the values in its URL, title
    and page, and every error path that quotes page text: nothing carries them."""
    page = _page(url=f"https://shop.example/pay?cc={PAN}&exp={EXP}", title=f"Pay {CVC}")
    page.evaluate = AsyncMock(return_value=PAN)
    _drive(page)
    await _arm_in("armed", page)
    records: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(self.format(record))

    grab = Grab()
    grab.addFilter(CARD_FILTER)
    root = logging.getLogger()
    root.addHandler(grab)
    try:
        f = tmp_path / "d.txt"
        f.write_text("x")
        monkeypatch.setattr(browser, "_declares_new_tab", AsyncMock(return_value=False))
        monkeypatch.setattr(
            browser, "_stealth_click", AsyncMock(side_effect=RuntimeError(f"<b>{CVC}</b>"))
        )
        results = [
            await browser._impl_browser_snapshot(),
            await browser._impl_browser_run_js("document.forms[0].cc.value"),
            await browser._impl_browser_screenshot(),
            await browser._impl_browser_fill("#cc", "x"),
            await browser._impl_browser_upload("#f", str(f)),
            await browser._impl_browser_press_key("Control+c"),
            await browser._impl_browser_press_key("Tab"),
            await browser._impl_browser_click("#pay"),
        ]
        with patch.object(browser, "_get_page", AsyncMock(return_value=(page, False))):
            results.append(await browser._impl_browser_navigate("https://shop.example/"))
        logging.getLogger("genesis.mcp.health.browser").error(
            "page said %s", f"{PAN} {CVC}", exc_info=True
        )
    finally:
        root.removeHandler(grab)
    _no_secret(results)
    _no_secret(records)
    assert records  # the log line above was captured, args withheld
    assert not browser._TS_LOG_PATH.exists()
