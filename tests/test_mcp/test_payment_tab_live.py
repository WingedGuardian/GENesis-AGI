"""Live tests of the payment-tab guard against a real Camoufox (opt-in).

Run with ``GENESIS_LIVE_BROWSER_TESTS=1``; skipped otherwise, and when Camoufox,
the X display :99, scrot or xdotool is missing. Each test launches Camoufox
headed on :99 through browser.py's own launch path, against a local checkout
page on 127.0.0.1 with a cross-origin card iframe on 127.0.0.2.

The harness (this file) fills Stripe's public test numbers with Playwright and
is the only place they appear: every tool result and log line is checked for
them. The harness's own reads (``input_value``) prove what the tools must not
show is really there.

L1 Back (top level), L2 Back (iframe), L3 run_js, L4 finish, L7 the screen,
L8 the context taint: see the plan for PR7b.
"""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
import logging
import os
import shutil
import sqlite3
import struct
import subprocess
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from genesis.mcp.health import browser, payment_tab
from genesis.mcp.health.payment_tab import TABS, PaymentTabError
from genesis.security.card_redaction import CARD_FILTER

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("GENESIS_LIVE_BROWSER_TESTS") != "1",
        reason="live Camoufox tests: set GENESIS_LIVE_BROWSER_TESTS=1",
    ),
    pytest.mark.asyncio,
]

PAN = "4242424242424242"
FRAME_PAN = "4000002500003155"
EXP = "12/34"
CVC = "739"
NAME = "Pat Tester"
SECRETS = (PAN, FRAME_PAN, EXP, CVC, "Tester")
DISPLAY = ":99"
PROD_PROFILE = Path.home() / ".genesis" / "camoufox-profile"
WORK = Path.home() / "tmp" / "payment-tab-live"

_STYLE = (
    "<style>body{margin:0;background:#fff;font:16px sans-serif}"
    "input{display:block;width:420px;height:44px;margin:6px;border:0;outline:0;"
    "background:#fff;color:#000;font:28px monospace;padding:0 6px}"
    "#mark{position:absolute;left:700px;top:8px;width:24px;height:24px;background:#f0f}"
    "iframe{border:0;width:460px;height:240px}</style>"
)
_FIELDS = (
    "<input id=name name=ccname autocomplete=cc-name aria-label='Name on card'>"
    "<input id=num name=cardnumber autocomplete=cc-number aria-label='Card number'>"
    "<input id=exp name=exp autocomplete=cc-exp aria-label='Expiry'>"
    "<input id=cvc name=cvc autocomplete=cc-csc aria-label='CVC'>"
)
_PROBE = "<script>localStorage.setItem('genesis_probe','1')</script>"


def _checkout(frame_port: int) -> str:
    return (
        f"<!doctype html><title>checkout</title>{_STYLE}<div id=mark></div>"
        f"<form id=f action=/done method=get>{_FIELDS}<button id=pay>Pay</button></form>"
        f"<iframe id=card src='http://127.0.0.2:{frame_port}/frame'></iframe>"
        f"<p><a id=away href=/away>away</a></p>{_PROBE}"
    )


def _pages(frame_port: int):
    return {
        "/checkout": _checkout(frame_port),
        "/frame": f"<!doctype html><title>frame</title>{_STYLE}<form>{_FIELDS}</form>{_PROBE}",
        "/away": "<!doctype html><title>away</title><p>away</p>",
        "/done": "<!doctype html><title>done</title><p>thanks</p>",
        "/blank": "<!doctype html><title>blank</title>",
        "/blankframe": f"<!doctype html><title>bf</title><iframe src='http://127.0.0.2:{frame_port}/blank'></iframe>",
        "/opener": "<!doctype html><title>opener</title><button id=go>go</button>",
    }


def _serve(host: str, pages):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = pages().get(self.path.split("?")[0], "<!doctype html>").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer((host, 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture(scope="module")
def site():
    pytest.importorskip("camoufox")
    from genesis.browser import engine

    if not engine.resolve_camoufox_engine().ready:
        pytest.skip("no launchable Camoufox engine")
    if not Path("/tmp/.X11-unix/X99").exists():
        pytest.skip("no X display :99")
    for tool in ("scrot", "xdotool"):
        if shutil.which(tool) is None:
            pytest.skip(f"{tool} not installed")
    holder: dict = {}
    b = _serve("127.0.0.2", lambda: _pages(holder["fport"]))
    holder["fport"] = b.server_address[1]
    a = _serve("127.0.0.1", lambda: _pages(holder["fport"]))
    yield f"http://127.0.0.1:{a.server_address[1]}"
    a.shutdown()
    b.shutdown()


def _fresh_profile(name: str, from_prod: bool = False) -> Path:
    dest = WORK / name
    shutil.rmtree(dest, ignore_errors=True)
    if from_prod and PROD_PROFILE.is_dir():
        shutil.copytree(
            PROD_PROFILE,
            dest,
            symlinks=True,
            # Without its session files: Ctrl+Shift+T with nothing closed restores
            # the previous session, which would reload the live profile's tabs,
            # and the recovery file checked afterwards must be this run's own.
            ignore=shutil.ignore_patterns(
                "cache2", "startupCache", "lock", ".parentlock", "sessionstore*", "previous.jsonlz4"
            ),
        )
    else:
        dest.mkdir(parents=True)
    return dest


class _Logs(logging.Handler):
    def __init__(self):
        super().__init__()
        self.lines: list[str] = []
        self.addFilter(CARD_FILTER)

    def emit(self, record):
        self.lines.append(self.format(record))


@contextlib.asynccontextmanager
async def _browser(monkeypatch, profile: Path, card_fill: bool = True):
    from genesis.browser import profile_backup

    monkeypatch.setattr(browser, "_PROFILE_DIR", profile)
    monkeypatch.setattr(payment_tab, "card_fill_enabled", lambda: card_fill)
    monkeypatch.setattr(profile_backup, "backup_camoufox_profile", lambda *a: None)
    monkeypatch.setattr(browser, "_vnc_verified", True)
    monkeypatch.setattr(browser, "_human_delay", AsyncMock())  # pacing is not under test
    monkeypatch.setattr(browser, "_TS_LOG_PATH", profile.parent / f"{profile.name}-ts.log")
    monkeypatch.setattr(browser, "_browser_lock", asyncio.Lock())
    TABS.reset()
    logs = _Logs()
    logging.getLogger("genesis").addHandler(logs)
    try:
        yield logs
    finally:
        logging.getLogger("genesis").removeHandler(logs)
        await browser.async_cleanup()
        ts = browser._TS_LOG_PATH
        _no_secret(ts.read_text() if ts.exists() else "")
        _no_secret(logs.lines)


def _no_secret(blob) -> None:
    text = blob if isinstance(blob, str) else json.dumps(blob, default=str)
    for s in SECRETS:
        assert s not in text, f"a filled value reached a tool result or log ({s[:2]}..)"


async def _fill(page) -> None:
    """The harness's fill (PR7b-2's job): top level and the card iframe."""
    payment_tab.begin_fill(page)
    frame = page.frame_locator("#card")
    for target, pan in ((page, PAN), (frame, FRAME_PAN)):
        await target.locator("#name").fill(NAME)
        await target.locator("#num").fill(pan)
        await target.locator("#exp").fill(EXP)
        await target.locator("#cvc").fill(CVC)
    payment_tab.mark_filled(page, (NAME, PAN, FRAME_PAN, EXP, CVC))


async def _values(page) -> dict:
    frame = page.frame_locator("#card")
    return {
        "top": await page.locator("#num").input_value(),
        "frame": await frame.locator("#num").input_value(),
        "top_cvc": await page.locator("#cvc").input_value(),
    }


async def _open_checkout(site) -> object:
    result = await browser._impl_browser_navigate(f"{site}/checkout")
    assert "error" not in result, result
    page = browser._active_page
    await page.frame_locator("#card").locator("#num").wait_for()
    return page


async def _back(page) -> None:
    await page.go_back(wait_until="load")
    await page.frame_locator("#card").locator("#num").wait_for()
    await asyncio.sleep(1.5)  # form restoration and paint


# ---------------------------------------------------------------------------
# L1 / L2 / L3
# ---------------------------------------------------------------------------


async def test_l1_l2_l3_back_restores_values_and_no_tool_shows_them(site, monkeypatch):
    profile = _fresh_profile("l1")
    async with _browser(monkeypatch, profile) as logs:
        page = await _open_checkout(site)
        assert await payment_tab.arm(page) is True  # mask verified in both frames
        await _fill(page)
        results = [await browser._impl_browser_snapshot()]
        clicked = await browser._impl_browser_click("#pay")  # a GET submit: values in the URL
        results.append(clicked)
        assert clicked["url"] == f"{site}/done", clicked  # path-only
        await _back(page)
        restored = await _values(page)
        # The harness proves Back restored the values (L1 top, L2 iframe) ...
        assert restored == {"top": PAN, "frame": FRAME_PAN, "top_cvc": CVC}
        assert await TABS.verify_mask(page) is True  # re-masked on the new document
        # ... and no tool output carries them.
        results += [
            await browser._impl_browser_snapshot(),
            await browser._impl_browser_screenshot(),
            await browser._impl_browser_fill("#num", "1"),
            await browser._impl_browser_press_key("Control+a"),
            await browser._impl_browser_press_key("Tab"),
            await browser._impl_browser_navigate(f"{site}/away"),
            # L3: run_js refused, aimed at the iframe too
            await browser._impl_browser_run_js("document.querySelector('#num').value"),
            await browser._impl_browser_run_js(
                "document.getElementById('card').contentWindow.document.querySelector('#num').value"
            ),
        ]
        assert results[-1] == {"error": browser._REFUSE_PAYMENT_RUN_JS}
        assert results[-2] == {"error": browser._REFUSE_PAYMENT_RUN_JS}
        assert results[3]["error"] == browser._REFUSE_PAYMENT_TAB  # screenshot
        snap = results[2]["snapshot"]
        assert "[hidden: payment tab]" in snap
        _no_secret(results)
        print("\nL1/L2/L3 results:", json.dumps(results, default=str)[:1500])
        assert logs is not None


# ---------------------------------------------------------------------------
# L7: the screen
# ---------------------------------------------------------------------------


def _scrot(path: Path) -> object:
    from PIL import Image

    subprocess.run(
        ["scrot", "-o", str(path)], env={**os.environ, "DISPLAY": DISPLAY}, check=True, timeout=30
    )
    return Image.open(path).convert("RGB")


def _offset(img, mark_box) -> tuple[int, int]:
    """Screen position minus page position, from the magenta marker."""
    w, h = img.size
    px = img.load()
    hits = [(x, y) for y in range(0, min(h, 400)) for x in range(w) if px[x, y] == (255, 0, 255)]
    assert hits, "marker not on screen: the capture does not show the browser"
    return min(x for x, _ in hits) - round(mark_box["x"]), min(y for _, y in hits) - round(
        mark_box["y"]
    )


def _dark(img, box, off, inset: int = 5) -> int:
    px = img.load()
    x0, y0 = round(box["x"]) + off[0] + inset, round(box["y"]) + off[1] + inset
    x1, y1 = (
        round(box["x"] + box["width"]) + off[0] - inset,
        round(box["y"] + box["height"]) + off[1] - inset,
    )
    return sum(1 for y in range(y0, y1) for x in range(x0, x1) if max(px[x, y]) < 110)


async def test_l7_the_screen_after_back_shows_no_glyphs(site, monkeypatch):
    profile = _fresh_profile("l7")
    async with _browser(monkeypatch, profile):
        page = await _open_checkout(site)
        assert await payment_tab.arm(page) is True
        await _fill(page)
        await page.click("#away")  # the harness navigates away, then Back
        await page.wait_for_load_state("load")
        await _back(page)
        assert (await _values(page))["top"] == PAN  # restored under the mask
        assert await TABS.verify_mask(page) is True
        frame = page.frame_locator("#card")
        boxes = {
            "top_num": await page.locator("#num").bounding_box(),
            "top_cvc": await page.locator("#cvc").bounding_box(),
            "frame_num": await frame.locator("#num").bounding_box(),
            "frame_cvc": await frame.locator("#cvc").bounding_box(),
        }
        mark = await page.locator("#mark").bounding_box()
        masked_img = _scrot(WORK / "l7-masked.png")
        off = _offset(masked_img, mark)
        masked = {k: _dark(masked_img, b, off) for k, b in boxes.items()}
        # Control: the same screen with the mask removed must show glyphs.
        for f in page.frames:
            await f.evaluate(
                "() => { const s = document.getElementById('__genesis_payment_mask__'); if (s) s.remove(); }"
            )
        await asyncio.sleep(1.0)
        control_img = _scrot(WORK / "l7-control.png")
        control = {k: _dark(control_img, b, _offset(control_img, mark)) for k, b in boxes.items()}
        print(f"\nL7 dark pixels in field boxes: masked={masked} control={control}")
        assert all(v > 50 for v in control.values()), (
            f"control shows no glyphs: instrument void {control}"
        )
        assert all(v == 0 for v in masked.values()), f"glyph pixels under the mask: {masked}"


# ---------------------------------------------------------------------------
# L4: finish
# ---------------------------------------------------------------------------


def _lz4_block(src: bytes, size: int) -> bytes:
    out = bytearray()
    i = 0
    while i < len(src):
        tok = src[i]
        i += 1
        lit = tok >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        out += src[i : i + lit]
        i += lit
        if i >= len(src):
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = tok & 15
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(out) - off
        for k in range(ml):
            out.append(out[start + k])
    return bytes(out[:size])


def _mozlz4(path: Path) -> str | None:
    data = path.read_bytes()
    if not data.startswith(b"mozLz40\0"):
        return None
    (size,) = struct.unpack("<I", data[8:12])
    return _lz4_block(data[12:], size).decode("utf-8", "replace")


def _profile_hits(profile: Path) -> dict:
    hits: dict = {"sessionstore": [], "raw": [], "formhistory": [], "decoded": []}
    for p in profile.rglob("*"):
        if not p.is_file():
            continue
        if "lz4" in p.name:
            with contextlib.suppress(Exception):
                txt = _mozlz4(p) or ""
                if "sessionstore" in str(p) and txt:
                    hits["decoded"].append(p.name)
                hits["sessionstore"] += [
                    f"{p.name}:{s[:2]}" for s in (PAN, FRAME_PAN, f'"{CVC}"', EXP) if s in txt
                ]
        with contextlib.suppress(OSError):
            raw = p.read_bytes()
            if PAN.encode() in raw or FRAME_PAN.encode() in raw:
                hits["raw"].append(str(p.relative_to(profile)))
    fh = profile / "formhistory.sqlite"
    if fh.exists():
        con = sqlite3.connect(f"file:{fh}?mode=ro", uri=True)
        try:
            rows = con.execute("select value from moz_formhistory").fetchall()
        finally:
            con.close()
        hits["formhistory_rows"] = len(rows)
        hits["formhistory"] = [v[:1] for (v,) in rows if v in (PAN, FRAME_PAN, CVC, EXP, NAME)]
    return hits


async def _undo_close(ctx, presses: int = 3) -> dict:
    """Press Ctrl+Shift+T in the browser window as an X key event (the
    browser's own shortcut; a Playwright key press does not reach it, see
    test_l4) up to ``presses`` times, until the checkout is back. Judged by
    URL, since Firefox restores into a selected blank tab. A restored checkout
    is read by the harness, before and after a Back.

    The waits are MEASURED: with 1 s after the close and 3 s after the key, an
    http tab was not restored even with undo enabled; with 6 s and 6 s it was."""
    env = {**os.environ, "DISPLAY": DISPLAY}
    out: dict = {"added_pages": 0, "checkout_restored": False, "values_restored": False}
    before = len(ctx.pages)
    await asyncio.sleep(6)
    for _ in range(presses):
        subprocess.run(
            ["xdotool", "mousemove", "960", "600", "key", "--clearmodifiers", "ctrl+shift+t"],
            env=env,
            check=True,
            timeout=10,
        )
        await asyncio.sleep(6)
        if any(_is_checkout_tab(p) for p in ctx.pages):
            break
    out["added_pages"] = len(ctx.pages) - before
    out["restored_paths"] = sorted(p.url.split("?")[0].split("/", 3)[-1] for p in ctx.pages)
    for p in [p for p in ctx.pages if _is_checkout_tab(p)]:
        out["checkout_restored"] = True
        got = []
        with contextlib.suppress(Exception):
            if "/away" in p.url:  # restored at its current entry: Back to the form
                await p.go_back(wait_until="load")
                await asyncio.sleep(1.5)
            got.append(await p.locator("#num").input_value(timeout=3000))
            await p.click("#away")
            await p.wait_for_load_state("load")
            await p.go_back(wait_until="load")
            await asyncio.sleep(1.5)
            got.append(await p.locator("#num").input_value(timeout=3000))
        out["values_restored"] = out["values_restored"] or PAN in got
    return out


def _is_checkout_tab(page) -> bool:
    path = page.url.split("?")[0]
    return path.endswith("/checkout") or path.endswith("/away")


async def _storage_probe(ctx, site) -> dict:
    p = await ctx.new_page()
    try:
        await p.goto(f"{site}/blankframe", wait_until="load")
        await asyncio.sleep(0.5)
        return {
            "top": await p.evaluate("() => localStorage.getItem('genesis_probe')"),
            "frame": await p.frames[1].evaluate("() => localStorage.getItem('genesis_probe')"),
        }
    finally:
        await p.close()


# Firefox's stock values. Camoufox's own camoufox.cfg defaults max_tabs_undo
# to 0, privacy_level to 2 and resume_from_crash to false (MEASURED,
# 156.0.1-beta.36). The control pins the stock ones, under which the session
# store keeps form data, so a restored tab that comes back filled shows the
# instrument can see it.
_STOCK_SESSIONSTORE = {
    "browser.sessionstore.max_tabs_undo": 25,
    "browser.sessionstore.privacy_level": 0,
    "browser.sessionstore.resume_from_crash": True,
}


async def _l4_run(site, monkeypatch, name: str) -> dict:
    profile = _fresh_profile(name, from_prod=True)
    out: dict = {"prod_copy_formhistory": (profile / "formhistory.sqlite").exists()}
    async with _browser(monkeypatch, profile):
        page = await _open_checkout(site)
        ctx = page.context
        out["storage_before"] = await _storage_probe(ctx, site)
        assert await payment_tab.arm(page) is True
        await _fill(page)
        # Closed with the filled form as the current entry: the one entry whose
        # form data a session store keeps (the worst case for a restore).
        out["finish"] = await payment_tab.finish(page)
        out["page_closed"] = page.is_closed()
        fresh = browser._active_page
        out["tools_on_live_page"] = (
            fresh is not None and fresh is not page and not fresh.is_closed()
        )
        out["run_js_after"] = await browser._impl_browser_run_js("1+1")
        # The agent's own key path: browser_press_key on the page it is left on.
        await asyncio.sleep(6)
        out["press_key"] = await browser._impl_browser_press_key("Control+Shift+T")
        await asyncio.sleep(6)
        out["press_key_restored"] = any(_is_checkout_tab(p) for p in ctx.pages)
        out["x_undo"] = await _undo_close(ctx)
        out["storage_after"] = await _storage_probe(ctx, site)
        await asyncio.sleep(18)  # the session store writes every 15 s
        out["profile"] = _profile_hits(profile)
    return out


async def _l4_control(site, monkeypatch) -> dict:
    """The instrument's control: the same fill, the tab closed with no guard at
    all, under the stock session-store prefs. Undo-close must bring it back."""
    profile = _fresh_profile("l4-control")
    monkeypatch.setattr(
        payment_tab, "CARD_FILL_PREFS", {**payment_tab.CARD_FILL_PREFS, **_STOCK_SESSIONSTORE}
    )
    async with _browser(monkeypatch, profile):
        page = await _open_checkout(site)
        ctx = page.context
        keep = await ctx.new_page()
        browser._active_page = browser._stealth_page = keep
        frame = page.frame_locator("#card")
        for target, pan in ((page, PAN), (frame, FRAME_PAN)):
            await target.locator("#num").fill(pan)
        await page.close(run_before_unload=False)
        return await _undo_close(ctx)


async def test_l4_finish_leaves_nothing_restorable(site, monkeypatch):
    """After finish: the tab is gone, the tools are on a live page, run_js runs
    again, the site storage is cleared, and no undo-close path brings a value
    back. Camoufox restores a closed tab's URL on an X Ctrl+Shift+T even with
    max_tabs_undo=0 (MEASURED), so the claim is about VALUES, with a stock
    session-store control that must restore them."""
    out = await _l4_run(site, monkeypatch, "l4")
    control = await _l4_control(site, monkeypatch)
    print(
        f"\nL4 treatment: {json.dumps(out, default=str)}\nL4 control: {json.dumps(control, default=str)}"
    )
    assert out["prod_copy_formhistory"] is True  # the copy really has a formhistory.sqlite
    assert out["finish"] == {"closed": True, "storage_cleared": True}
    assert out["page_closed"] and out["tools_on_live_page"]
    assert out["run_js_after"].get("result") == 2
    assert out["storage_before"] == {"top": "1", "frame": "1"}  # the probe sees storage
    assert out["storage_after"] == {"top": None, "frame": None}
    assert not out["press_key_restored"]  # no agent key path restores the tab
    # The control shows the X key path reaches the browser and brings a closed
    # checkout back; L1 shows the same input_value read sees values when they
    # are there. No configuration tried (Camoufox's defaults, these prefs, the
    # stock session store) restored values through undo-close (MEASURED), so
    # the control's own values are recorded, not required.
    assert control["checkout_restored"], f"undo-close instrument void: {control}"
    assert not out["x_undo"]["values_restored"]
    assert out["profile"]["sessionstore"] == [] and out["profile"]["raw"] == []
    assert out["profile"]["formhistory"] == []


# ---------------------------------------------------------------------------
# L8: the context taint (the two-tab opener exploit)
# ---------------------------------------------------------------------------

# What a planted script looks like: run_js (an isolated world on Camoufox)
# inserts a <script>, which runs in the page's own world, so the popup it opens
# on the next click is a normal tab and it keeps a handle on it. The handle is
# all it needs to read tab A's fields once they are filled.
_PLANTED = (
    "document.getElementById('go').addEventListener('click', () => {"
    " window.__w = window.open('/checkout', 'payA');"
    " setInterval(() => { try {"
    " const v = [...window.__w.document.querySelectorAll('input')].map(i => i.value).join(' ');"
    " let p = document.getElementById('loot');"
    " if (!p) { p = document.createElement('p'); p.id = 'loot'; document.body.appendChild(p); }"
    " p.textContent = 'loot ' + v; } catch (e) {} }, 200); });"
)
_PLANT = (
    "(() => { const s = document.createElement('script'); s.textContent = "
    + json.dumps(_PLANTED)
    + "; document.head.appendChild(s); return 'planted'; })()"
)


async def test_l8_a_planted_opener_script_refuses_arming(site, monkeypatch):
    profile = _fresh_profile("l8")
    async with _browser(monkeypatch, profile):
        nav = await browser._impl_browser_navigate(f"{site}/opener")
        tab_b = browser._active_page
        assert "error" not in nav
        assert (await browser._impl_browser_run_js(_PLANT))["result"] == "planted"
        await browser._impl_browser_click("#go")  # B opens A
        ctx = tab_b.context
        for _ in range(50):
            tab_a = next((p for p in ctx.pages if p.url.endswith("/checkout")), None)
            if tab_a is not None:
                break
            await asyncio.sleep(0.2)
        assert tab_a is not None and tab_a is not tab_b, [p.url for p in ctx.pages]
        await tab_a.frame_locator("#card").locator("#num").wait_for()
        with pytest.raises(PaymentTabError) as refused:
            await payment_tab.arm(tab_a)
        assert refused.value.code == "browser_tainted_restart"
        # Control, harness only: without the taint check, is the exploit real?
        TABS._tainted = None
        await payment_tab.arm(tab_a)
        await _fill(tab_a)
        await asyncio.sleep(1.5)
        loot = await tab_b.locator("#loot").text_content(timeout=5000)
        exploit_reached_b = PAN in (loot or "")
        browser._active_page = browser._stealth_page = tab_b
        while_armed = await browser._impl_browser_snapshot()
        _no_secret(while_armed)  # HMAC tokens hide it while A is armed
        await payment_tab.finish(tab_a)
        browser._active_page = browser._stealth_page = tab_b
        after_finish = await browser._impl_browser_snapshot()
        leaked_after = any(s in json.dumps(after_finish) for s in (PAN, CVC))
        print(
            f"\nL8: arm refused={refused.value.code}; control: planted script copied A's values "
            f"into B={exploit_reached_b}; B snapshot while armed clean=True; "
            f"B snapshot after finish carries them={leaked_after}"
        )
        assert exploit_reached_b, "control void: the planted script did not reach tab A"
