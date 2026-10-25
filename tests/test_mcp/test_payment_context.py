"""browser_fill_card's flow (genesis.mcp.health.payment_context) against a fake
vault socket and a fake two-context browser, plus its browser.py hooks.

The vault side is a real unix socket speaking vault_service's protocol (one
JSON line each way), scripted per test; the release state machine has its own
tests (tests/test_payments). The browser is a small fake of the Playwright
surface the flow uses: a main context whose ``browser.new_context`` makes the
payment context, frames, locators, element handles, fill, select_option,
input_value and click. A live run is the harness E2E, not here.

The only card number is Stripe's public test Visa. The CVC, name and postal
code are generated per run.
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import secrets
import string
import sys
import types
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import quote

import pytest

from genesis.browser.types import BrowserLayer
from genesis.mcp.health import browser
from genesis.mcp.health import payment_context as pc
from genesis.payments import card_fill as cf
from genesis.payments import release_client

PAN = "4242424242424242"
CARD_REF = "card_" + "ab12" * 3
# frozen-clock-ok: 3-year margin; the test expiry stays valid for any run shorter than that
YEAR = datetime.now(UTC).year + 3
CVC = "".join(secrets.choice(string.digits[1:]) for _ in range(3))
NAME = "Zq" + "".join(secrets.choice(string.ascii_lowercase) for _ in range(8))
POSTAL = "Q" + "".join(secrets.choice(string.digits) for _ in range(4)) + "X"
EMAIL = "zq" + "".join(secrets.choice(string.ascii_lowercase) for _ in range(6)) + "@example.org"
SECRETS = (PAN, "4242 4242 4242 4242", CVC, NAME, POSTAL, EMAIL, f"12/{YEAR % 100:02d}")
SHOP = "https://shop.example.com/checkout?step=pay"
CARDS = "https://js.cards.example.net/frame"
# camoufox.utils.STOCK_MEDIA_DEFAULTS as of camoufox 0.5.8, for runs without the package.
MEDIA_DEFAULTS = {
    "color_scheme": "no-override",
    "reduced_motion": "no-override",
    "forced_colors": "no-override",
    "contrast": "no-override",
}


def _sweep(*blobs) -> None:
    for blob in blobs:
        text = blob if isinstance(blob, str) else json.dumps(blob, default=str)
        for s in SECRETS:
            assert s not in text, f"a value reached a result or log ({s[:2]}..)"


# ---------------------------------------------------------------------------
# The fake browser
# ---------------------------------------------------------------------------


class El:
    def __init__(self, tag="input", kinds=(), options=(), child=None, pay=None, extra=None):
        self.tag, self.kinds, self.options, self.child = tag, set(kinds), list(options), child
        self.pay = pay  # PAY_JS's answer, for a Pay control
        self.extra = extra  # EXTRA_FIELD_JS's answer
        self.value = ""
        self.mangle = False  # the page shows something other than what was typed
        self.on_click = None
        self.owner = None  # the frame the element really lives in (None: the one it was found in)
        self.labels: dict = {}  # a select's option value -> label, where the two differ


class Handle:
    def __init__(self, el, frame=None):
        self.el = el
        self.frame = frame

    async def owner_frame(self):
        return self.el.owner if self.el.owner is not None else self.frame

    async def content_frame(self):
        return self.el.child

    async def dispose(self):
        pass

    async def evaluate(self, js, arg=None):
        if js is pc.CARD_FIELD_JS:
            return self.el.tag if arg in self.el.kinds else ""
        if js is pc.PAY_JS:
            return dict(self.el.pay)
        if js is pc.EXTRA_FIELD_JS:
            return self.el.extra or self.el.tag
        if js is pc._SAME_JS:
            return arg.el is self.el
        if js is pc.EXTRA_VALUE_JS:
            return [
                self.el.value,
                *([self.el.labels[self.el.value]] if self.el.value in self.el.labels else []),
            ]
        raise AssertionError("unexpected script")

    async def fill(self, value, timeout=None):
        self.el.value = "x" if self.el.mangle else value

    async def select_option(self, timeout=None, value=None, label=None):
        if label is not None:
            by_label = {lab: v for v, lab in self.el.labels.items()}
            want = by_label.get(label, label)
        else:
            want = value
        if want not in self.el.options:
            raise RuntimeError("no option")
        self.el.value = want

    async def input_value(self, timeout=None):
        return self.el.value

    async def click(self, timeout=None):
        if self.el.on_click is not None:
            self.el.on_click()


class Loc:
    def __init__(self, frame, sel):
        self.frame, self.sel = frame, sel

    @property
    def first(self):
        return self

    async def wait_for(self, state=None, timeout=None):
        # A widget that mounts late: its elements appear when something waits.
        self.frame.waits.append(self.sel)
        late = self.frame.late.pop(self.sel, None)
        if late is not None:
            self.frame.elements[self.sel] = late
        if not self.frame.elements.get(self.sel):
            raise TimeoutError("not attached")

    async def count(self):
        return len(self.frame.elements.get(self.sel, []))

    async def element_handle(self, timeout=None):
        return Handle(self.frame.elements[self.sel][0], self.frame)


class Frame:
    def __init__(self, url, elements=None, text=""):
        self.url, self.elements, self.text = url, elements or {}, text
        self.late: dict = {}  # selector -> elements that mount only once waited for
        self.waits: list[str] = []
        self.masked = True
        self.repairable = True  # MASK_INIT_JS evaluated into it takes
        self.mask_absent = False  # no mask at all (the init script never ran here)
        self.injected = 0
        self.parent_frame = None
        self.mask_raises = 0  # the next N mask checks raise (a frame still navigating)
        self.session: dict = {}
        self.visible = False  # its iframe element is shown (a revealed bank frame)

    def locator(self, sel):
        return Loc(self, sel)

    async def frame_element(self):
        frame = self

        class FrameEl:
            async def is_visible(self):
                return frame.visible

        return FrameEl()

    async def evaluate(self, js):
        if js == pc._MASK_CHECK_JS:
            if self.mask_raises:
                self.mask_raises -= 1
                raise RuntimeError("Execution context was destroyed")
            return self.masked
        if js == pc._MASK_ABSENT_JS:
            return self.mask_absent
        if js == pc.MASK_INIT_JS:
            self.injected += 1
            if self.repairable:
                self.masked, self.mask_absent = True, False
            return None
        if js == pc._TEXT_JS:
            return self.text
        if js == pc._SESSION_JS:
            return dict(self.session)
        raise AssertionError("unexpected script")


class Page:
    def __init__(self, url, frames=(), title="Checkout"):
        self.url = url
        self.main_frame = Frame(url)
        self.extra = list(frames)
        for f in self.extra:
            f.parent_frame = self.main_frame
        self._closed = False
        self._title = title
        self.handlers: dict = {}
        self.goto_to: str | None = None  # where goto lands (a redirect)
        self.context = None

    @property
    def frames(self):
        return [self.main_frame, *self.extra]

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)

    def is_closed(self):
        return self._closed

    async def title(self):
        return self._title

    async def goto(self, url, **_kw):
        self.url = self.main_frame.url = self.goto_to or url

    def navigate(self, url):
        """A new document: its request, then the commit."""
        self.request_navigation(url)
        self.same_document(url)

    def request_navigation(self, url):
        """A top-level navigation sent, not (yet) committed: an in-flight submit."""
        for fn in getattr(self.context, "handlers", {}).get("request", []):
            fn(_request(url, redirected=False, nav_frame=self.main_frame))

    def same_document(self, url):
        """A URL change with no request (a fragment, pushState, replaceState)."""
        self.url = self.main_frame.url = url
        for fn in self.handlers.get("framenavigated", []):
            fn(self.main_frame)


class PCtx:
    def __init__(self, page, log):
        self.page, self.log = page, log
        self.handlers: dict = {}
        self.closed = False
        self.close_fails = False
        page.context = self

    def on(self, event, fn):
        self.handlers.setdefault(event, []).append(fn)

    async def add_init_script(self, js):
        self.log.append(("init_script", js))
        log = self.log

        class Disposable:
            async def dispose(self):
                log.append(("dispose", js))

        return Disposable()

    async def route(self, pattern, handler):
        self.log.append(("route", pattern))
        self.route_handler = handler

    async def new_page(self):
        self.log.append(("new_page",))
        for fn in self.handlers.get("page", []):  # as Playwright: new_page() fires it too
            fn(self.page)
        return self.page

    async def close(self):
        if self.close_fails:
            raise RuntimeError("close failed")
        self.closed = True
        self.page._closed = True


class MainCtx:
    def __init__(self, pctx):
        self.pctx = pctx
        self.new_context_kwargs = None
        self.state = {
            "cookies": [
                {"name": "cart", "domain": ".example.com", "value": "c1"},
                {"name": "m", "domain": "js.cards.example.net", "value": "c2"},
                {"name": "other", "domain": ".unrelated.org", "value": "c3"},
            ],
            "origins": [
                {
                    "origin": "https://shop.example.com",
                    "localStorage": [{"name": "k", "value": "v"}],
                },
                {"origin": "https://unrelated.org", "localStorage": [{"name": "k", "value": "v"}]},
            ],
        }
        outer = self

        class _Browser:
            async def new_context(self, **kw):
                outer.new_context_kwargs = kw
                return outer.pctx

        self.browser = _Browser()

    async def storage_state(self, indexed_db=False):
        assert indexed_db is True
        self.exported = True
        return json.loads(json.dumps(self.state))


def checkout(select_expiry=False):
    """Main and payment pages with the number and CVC in a cross-origin iframe,
    the rest at top level, and a Pay button. Returns a namespace of parts."""

    def build():
        card_frame = Frame(CARDS)
        els = {
            "number": El(kinds={"number"}),
            "cvc": El(kinds={"cvc"}),
            "name": El(kinds={"name"}),
            "pay": El(
                tag="button",
                pay={"button": True, "get": False, "live": True, "label": "Pay $19.99"},
            ),
            "email": El(),
        }
        card_frame.elements = {"#num": [els["number"]], "#cvc": [els["cvc"]]}
        page = Page(SHOP, frames=[card_frame])
        page.main_frame.text = "Order total $19.99"
        page.main_frame.elements = {
            "iframe#card": [El(tag="iframe", child=card_frame)],
            "#name": [els["name"]],
            "#pay": [els["pay"]],
            "#email": [els["email"]],
        }
        if select_expiry:
            els["exp_month"] = El(
                tag="select", kinds={"exp_month"}, options=[str(m) for m in range(1, 13)]
            )
            els["exp_year"] = El(
                tag="select", kinds={"exp_year"}, options=[str(YEAR + i) for i in range(5)]
            )
            page.main_frame.elements["#mm"] = [els["exp_month"]]
            page.main_frame.elements["#yy"] = [els["exp_year"]]
        else:
            els["expiry"] = El(kinds={"expiry"})
            card_frame.elements["#exp"] = [els["expiry"]]
        return page, els

    main_page, _main_els = build()
    ppage, els = build()
    ppage.url = ppage.main_frame.url = "about:blank"
    log: list = []
    pctx = PCtx(ppage, log)
    main = MainCtx(pctx)
    fields = {
        "number": {"selector": "#num", "frames": ["iframe#card"]},
        "cvc": {"selector": "#cvc", "frames": ["iframe#card"]},
        "name": "#name",
    }
    if select_expiry:
        fields.update(exp_month="#mm", exp_year="#yy")
    else:
        fields["expiry"] = {"selector": "#exp", "frames": ["iframe#card"]}
    els["pay"].on_click = lambda: ppage.navigate("https://shop.example.com/done")

    class NS:
        pass

    ns = NS()
    ns.main_page, ns.ppage, ns.pctx, ns.main, ns.els, ns.fields, ns.log = (
        main_page,
        ppage,
        pctx,
        main,
        els,
        fields,
        log,
    )
    return ns


# ---------------------------------------------------------------------------
# The fake vault
# ---------------------------------------------------------------------------


class Vault:
    def __init__(self, path: Path):
        self.path = path
        self.requests: list[dict] = []
        self.statuses: list[dict] = [{"ok": True, "state": "granted", "expires_in_s": 120}]
        self.request_reply: dict = {"ok": True, "nonce": "nonce-1"}
        self.claim_reply: dict | None = None
        self.half_closed: list[str] = []
        self.on_status = None
        self.on_claim = None
        self.hold_request: asyncio.Event | None = None  # delays the request reply
        self.request_seen = asyncio.Event()
        self.server = None

    def ops(self, op):
        return [r for r in self.requests if r.get("op") == op]

    def card(self, keys=None):
        card = {
            "number": PAN,
            "exp_month": 12,
            "exp_year": YEAR,
            "cvc": CVC,
            "name": NAME,
            "postal_code": POSTAL,
            "label": "Test card",
            "last4": "4242",
        }
        req = self.ops("release_request")[-1]
        want = cf.reply_keys(req["field_origins"]) if keys is None else keys
        return {"ok": True, "card": {k: v for k, v in card.items() if k in want}}

    async def handle(self, reader, writer):
        req = json.loads(await reader.readline())
        self.requests.append(req)
        await asyncio.sleep(0)
        if reader.at_eof():  # the service reads a half-close as a gone client
            self.half_closed.append(req.get("op"))
        op = req.get("op")
        if op == "release_request":
            self.request_seen.set()
            if self.hold_request is not None:
                await self.hold_request.wait()
            reply = self.request_reply
        elif op == "release_status":
            if self.on_status is not None:
                self.on_status()
            reply = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
        elif op == "release_claim":
            if self.on_claim is not None:
                self.on_claim()
            reply = self.claim_reply if self.claim_reply is not None else self.card()
        elif op == "release_cancel":
            reply = {"ok": True}
        else:
            reply = {"ok": False, "error": "bad request"}
        writer.write((json.dumps(reply) + "\n").encode())
        await writer.drain()
        writer.close()


@pytest.fixture
async def vault(tmp_path, monkeypatch):
    v = Vault(tmp_path / "v.sock")
    v.server = await asyncio.start_unix_server(v.handle, path=str(v.path))
    monkeypatch.setattr(release_client, "vault_socket_path", lambda: v.path)
    yield v
    v.server.close()


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setattr(pc, "_flow", None)
    monkeypatch.setattr(pc, "_x_input", 0)
    monkeypatch.setattr(pc, "_prefs_pinned", True)
    for name, value in (
        ("POLL_S", 0.01),
        ("SETTLE_S", 0.3),
        ("NAV_GRACE_S", 0.05),
        ("TICK_S", 0.01),
        ("ATTACH_S", 0.05),
    ):
        monkeypatch.setattr(pc, name, value)
    # The flow reads Camoufox's media defaults for the payment context. CI's test
    # job installs no [browser] extra, and an absent package ends every flow at
    # checkout_load_failed, so stand in for it there; a real install is used as is.
    try:
        import camoufox.utils  # noqa: F401
    except ImportError:
        utils = types.ModuleType("camoufox.utils")
        utils.STOCK_MEDIA_DEFAULTS = MEDIA_DEFAULTS
        pkg = types.ModuleType("camoufox")
        pkg.utils = utils
        monkeypatch.setitem(sys.modules, "camoufox", pkg)
        monkeypatch.setitem(sys.modules, "camoufox.utils", utils)
    yield


@pytest.fixture
def logs():
    lines: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record):
            lines.append(record.getMessage() + repr(record.args) + repr(record.exc_info))

    h = Grab(level=logging.DEBUG)
    names = ("genesis.mcp.health.payment_context", "genesis.payments.release_client", "asyncio")
    for n in names:
        logging.getLogger(n).addHandler(h)
        logging.getLogger(n).setLevel(logging.DEBUG)
    yield lines
    for n in names:
        logging.getLogger(n).removeHandler(h)


class Hooks:
    def __init__(self):
        self.up = True
        self.close_browser = AsyncMock()
        self.interstitial = AsyncMock(return_value=False)

    def make(self):
        return pc.Hooks(
            alive=lambda: self.up,
            touch=lambda: None,
            interstitial=self.interstitial,
            close_browser=self.close_browser,
        )


def _args(ns, **over):
    kw = dict(
        card_ref=CARD_REF,
        amount="$19.99",
        fields=ns.fields,
        pay="#pay",
        expiry_format="MM/YY",
        extra_fields={"email": {"selector": "#email", "value": EMAIL}},
    )
    kw.update(over)
    return cf.validate(**kw)


async def _run(ns, hooks=None, report=None, **over):
    hooks = hooks or Hooks()
    out = await pc.fill_card(_args(ns, **over), ns.main, ns.main_page, hooks.make(), report)
    return out, hooks


# ---------------------------------------------------------------------------
# The flow
# ---------------------------------------------------------------------------


async def test_pays_in_an_isolated_context_and_returns_fixed_text(vault, logs):
    ns = checkout()
    progress: list[str] = []

    async def report(m):
        progress.append(m)

    vault.statuses = [{"ok": True, "state": "pending", "expires_in_s": 100}, vault.statuses[0]]
    out, _ = await _run(ns, report=report)

    assert out["code"] == "submitted_unverified"
    assert out["host"] == "shop.example.com"
    assert out["status"] == cf.TEXTS["submitted_unverified"]
    assert out["filled"] == ["number", "expiry", "cvc", "name"]
    assert set(out) == {"code", "status", "host", "filled"}
    # Typed in the payment context only; the main page is untouched.
    assert ns.els["number"].value == PAN and ns.els["cvc"].value == CVC
    assert ns.els["expiry"].value == f"12/{YEAR % 100:02d}"
    assert ns.els["email"].value == EMAIL
    assert all(e.value == "" for e in _main_values(ns))
    # The tap request: Pay's label, the extra field NAMES, the card fields' origins.
    req = vault.ops("release_request")[0]
    assert req["button_label"] == "Pay $19.99"
    assert req["also_filled"] == ["email"]
    assert req["field_origins"] == {
        "number": "https://js.cards.example.net",
        "expiry": "https://js.cards.example.net",
        "cvc": "https://js.cards.example.net",
        "name": "https://shop.example.com",
    }
    assert req["host"] == "shop.example.com" and req["amount"] == "$19.99"
    assert EMAIL not in json.dumps(req)
    # The context: mask script before the first page, viewport as the main launch,
    # merchant and card-frame storage only, closed at the end.
    # The network guard is routed before the first page loads.
    assert ns.log[:3] == [("init_script", pc.MASK_INIT_JS), ("route", "**/*"), ("new_page",)]
    kw = ns.main.new_context_kwargs
    assert kw["no_viewport"] is True and kw["accept_downloads"] is False
    assert kw["service_workers"] == "block"  # a worker's requests would skip the route
    assert {k: kw[k] for k in MEDIA_DEFAULTS} == MEDIA_DEFAULTS  # as the main launch
    assert sorted(c["name"] for c in kw["storage_state"]["cookies"]) == ["cart", "m"]
    assert [o["origin"] for o in kw["storage_state"]["origins"]] == ["https://shop.example.com"]
    assert ns.pctx.closed and not pc.in_flight()
    assert vault.ops("release_cancel") == [] and vault.half_closed == []
    # Progress waits for the tap, then keeps the client's idle clock fed to the end.
    assert progress[0].startswith("waiting for the owner's approval")
    after = progress[progress.index("finishing the approved payment") :]
    assert set(after) == {"finishing the approved payment"}
    _sweep(out, logs, progress)


class Route:
    """Playwright's Route, as the guard uses it."""

    def __init__(self, url, fail=False):
        self.request = types.SimpleNamespace(url=url)
        self.fail = fail
        self.done = None

    async def abort(self, error_code="failed"):
        self.done = ("abort", error_code)

    async def continue_(self):
        if self.fail:
            raise RuntimeError("route gone")
        self.done = ("continue",)


def _guard(number=PAN, **values):
    flow = pc._Flow(task=None)
    g = pc._UrlGuard(flow)
    g.arm({"number": number, **values})
    return flow, g


@pytest.mark.parametrize(
    "url",
    [
        f"https://shop.example.com/charge?card={PAN}",
        "https://shop.example.com/charge?card=4242+4242+4242+4242",
        "https://shop.example.com/charge?card=4242%204242%204242%204242",
        "https://shop.example.com/charge?card=4242-4242-4242-4242",
        "https://shop.example.com/charge?card=4242.4242.4242.4242",
        "https://shop.example.com/charge?card=4242%25204242%25204242%25204242",  # twice
        f"https://shop.example.com/{PAN}/x",
        "https://shop.example.com/c?card=4242%252B4242%252B4242%252B4242",  # '+' after decoding
        "https://shop.example.com/c?card=4242/4242/4242/4242",
        f"https://shop.example.com/c?x=99{PAN}77",  # inside a longer digit run
    ],
)
def test_the_url_guard_finds_the_card_number_in_a_url(url):
    _flow, g = _guard()
    assert g.carries(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "https://shop.example.com/charge?order=4242424242424241",  # one digit off
        "https://shop.example.com/charge?card=4242x4242x4242x4242",  # split by a letter
        "https://shop.example.com/charge?last4=4242&amount=1999",
        "https://shop.example.com/",
    ],
)
def test_the_url_guard_passes_other_urls(url):
    _flow, g = _guard()
    assert g.carries(url) is False


def test_an_unarmed_url_guard_passes_everything():
    g = pc._UrlGuard(pc._Flow(task=None))
    assert g.carries(f"https://shop.example.com/charge?card={PAN}") is False


async def test_the_url_guard_aborts_a_carrying_request_and_records_it():
    flow, g = _guard()
    r = Route(f"https://shop.example.com/charge?card={PAN}")
    await g.handle(r)
    assert r.done == ("abort", "blockedbyclient") and flow.url_blocked is True
    ok = Route("https://shop.example.com/charge?card=tok_abc")
    await g.handle(ok)
    assert ok.done == ("continue",)


async def test_the_url_guard_aborts_when_it_cannot_judge(monkeypatch, logs):
    flow, g = _guard()

    def boom(_url):
        raise ValueError(PAN)

    monkeypatch.setattr(g, "_judge", boom)
    r = Route("https://shop.example.com/x")
    await g.handle(r)
    assert r.done == ("abort", "failed")  # fail closed
    assert flow.url_blocked is False  # it could not tell: no claim that the card was in it
    _sweep(logs)


async def test_the_tap_shows_pay_as_it_reads_after_the_extra_fields(vault):
    # Filling an address can re-render the checkout: the tap must show Pay's
    # label as it stands after the extras, not as it read before them.
    ns = checkout()
    real_fill = Handle.fill

    async def fill(self, value, timeout=None):
        await real_fill(self, value, timeout)
        if self.el is ns.els["email"]:
            ns.els["pay"].pay.update(label="Pay $19.99 now")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Handle, "fill", fill)
        out, _ = await _run(ns)
    assert vault.ops("release_request")[0]["button_label"] == "Pay $19.99 now"
    assert out["code"] == "submitted_unverified"


async def test_a_url_too_heavy_to_judge_is_aborted_unrecorded():
    flow, g = _guard()
    heavy = "https://t.example.org/x?d=" + "1" * (pc._MAX_WINDOWS + 16)
    with pytest.raises(pc._Unjudgeable):
        g.carries(heavy)
    r = Route(heavy)
    await g.handle(r)
    assert r.done == ("abort", "failed") and flow.url_blocked is False
    # Right at the bound it is still judged.
    assert g.carries("https://t.example.org/x?d=" + "1" * (pc._MAX_WINDOWS + 15)) is False


async def test_a_failed_pass_on_is_logged_not_raised(logs):
    _flow, g = _guard()
    r = Route("https://shop.example.com/a.js", fail=True)
    await g.handle(r)
    assert r.done is None
    assert any("could not pass a request on" in line for line in logs)


def _request(url, redirected=True, nav_frame=None):
    return types.SimpleNamespace(
        url=url,
        redirected_from=object() if redirected else None,
        is_navigation_request=lambda: nav_frame is not None,
        frame=nav_frame,
    )


def test_a_redirect_hop_carrying_the_number_is_recorded_as_leaked():
    flow, g = _guard()
    g.on_request(_request(f"https://shop.example.com/land?pan={PAN}"))
    assert flow.url_leaked is True and flow.url_blocked is False


def test_a_first_request_or_a_clean_hop_is_not_a_leak():
    flow, g = _guard()
    g.on_request(_request(f"https://shop.example.com/c?pan={PAN}", redirected=False))  # routed
    g.on_request(_request("https://shop.example.com/land?order=1"))
    assert flow.url_leaked is False


def test_an_unjudgeable_redirect_hop_counts_as_a_leak_once_armed():
    flow, g = _guard()
    g.on_request(_request("https://t.example.org/x?d=" + "1" * (pc._MAX_WINDOWS + 16)))
    assert flow.url_leaked is True
    unarmed = pc._UrlGuard(pc._Flow(task=None))
    unarmed.on_request(_request("https://t.example.org/x?d=" + "1" * (pc._MAX_WINDOWS + 16)))
    assert unarmed._flow.url_leaked is False


async def test_a_card_number_redirect_after_pay_is_reported_as_leaked(vault, logs):
    ns = checkout()

    def pay():
        for fn in ns.pctx.handlers["request"]:
            fn(_request(f"https://shop.example.com/land?pan={PAN}"))

    ns.els["pay"].on_click = pay
    out, _ = await _run(ns)
    assert out["code"] == "card_url_leaked" and "do NOT retry" in out["status"]
    _sweep(out, logs)


async def test_a_card_number_url_after_pay_is_blocked_and_reported(vault, logs):
    ns = checkout()
    hit: list = []

    def pay():  # Pay makes the checkout send the card in a URL (a native GET form)
        r = Route(f"https://shop.example.com/charge?cardNumber={PAN}&cvc={CVC}")
        hit.append(r)
        asyncio.ensure_future(ns.pctx.route_handler(r))

    ns.els["pay"].on_click = pay
    out, _ = await _run(ns)
    assert hit[0].done == ("abort", "blockedbyclient")
    assert out["code"] == "card_url_blocked"
    assert "do NOT retry" in out["status"]
    assert ns.pctx.closed
    _sweep(out, logs)


async def test_a_card_number_url_while_typing_stops_before_pay(vault, logs):
    ns = checkout()
    clicked: list = []
    ns.els["pay"].on_click = lambda: clicked.append(1)
    real_fill = Handle.fill

    async def fill(self, value, timeout=None):
        await real_fill(self, value, timeout)
        if self.el is ns.els["number"]:  # an input listener sends the number in a URL
            await ns.pctx.route_handler(Route(f"https://t.example.org/c?n={PAN}"))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Handle, "fill", fill)
        out, _ = await _run(ns)
    assert out["code"] == "card_url_before_pay" and clicked == []
    assert "exposed" in out["status"] and "NOT clicked" in out["status"]
    # Nothing more is typed once the number went out: the next field stays empty.
    assert out["filled"] == ["number"] and ns.els["expiry"].value == ""
    _sweep(out, logs)


async def test_a_card_number_url_after_the_last_field_stops_before_pay(vault, logs):
    ns = checkout()
    clicked: list = []
    ns.els["pay"].on_click = lambda: clicked.append(1)
    real_input_value = Handle.input_value

    async def input_value(self, timeout=None):  # a script fires while the last field is read
        if self.el is ns.els["name"] and self.el.value:
            for fn in ns.pctx.handlers["request"]:
                fn(_request(f"https://shop.example.com/land?pan={PAN}"))
        return await real_input_value(self, timeout)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Handle, "input_value", input_value)
        out, _ = await _run(ns)
    assert out["code"] == "card_url_before_pay" and clicked == []
    _sweep(out, logs)


@pytest.mark.parametrize(
    ("blocked", "leaked", "code"),
    [
        (True, False, "card_url_blocked"),
        (False, True, "card_url_leaked"),
        (True, True, "card_url_leaked"),
    ],
)
async def test_the_outcome_ends_at_once_on_a_guard_flag(monkeypatch, blocked, leaked, code):
    # The settle wait is long; a flag ends it on the first tick, leaked before blocked.
    monkeypatch.setattr(pc, "SETTLE_S", 30.0)
    flow = pc._Flow(task=None, url_blocked=blocked, url_leaked=leaked)
    page = Page(SHOP)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    got = await pc._outcome(flow, page, SHOP, {SHOP}, 0, "example.com", Hooks().make())
    assert got == code and loop.time() - t0 < 5.0


async def test_a_flag_set_after_the_outcome_settles_still_wins(vault, monkeypatch):
    ns = checkout()
    real = pc._outcome

    async def late(flow, *a):
        code = await real(flow, *a)
        flow.url_blocked = True  # the route's abort lands after the last tick
        return code

    monkeypatch.setattr(pc, "_outcome", late)
    out, _ = await _run(ns)
    assert out["code"] == "card_url_blocked"


async def test_a_declared_get_form_is_refused_before_the_tap(vault):
    ns = checkout()
    ns.els["pay"].pay.update(get=True)
    out, _ = await _run(ns)
    assert out["code"] == "pay_get_form"
    assert vault.ops("release_request") == []


def _main_values(ns):
    els = []
    for frame in ns.main_page.frames:
        for group in frame.elements.values():
            els += [e for e in group if e.tag != "iframe"]
    return els


async def test_select_expiry_is_matched_to_an_option(vault):
    ns = checkout(select_expiry=True)
    out, _ = await _run(ns)
    assert out["code"] == "submitted_unverified"
    assert ns.els["exp_month"].value == "12" and ns.els["exp_year"].value == str(YEAR)


async def test_a_challenge_frame_after_pay_closes_at_once(vault, logs):
    ns = checkout()

    def challenge():  # the page moves on within the site AND shows a bank frame
        ns.ppage.extra.append(Frame("https://acs.bank.example/x"))
        ns.ppage.navigate("https://shop.example.com/processing")

    ns.els["pay"].on_click = challenge
    out, _ = await _run(ns)
    assert out["code"] == "challenge"
    assert "do NOT retry" in out["status"]
    assert ns.pctx.closed and not pc.in_flight()
    _sweep(out, logs)


async def test_no_settle_is_a_challenge_never_not_paid(vault):
    ns = checkout()
    ns.els["pay"].on_click = None
    out, _ = await _run(ns)
    assert out["code"] == "challenge"


async def test_a_navigation_off_the_merchant_site_is_a_challenge(vault):
    ns = checkout()
    ns.els["pay"].on_click = lambda: ns.ppage.navigate("https://bank-verify.example.org/start")
    out, _ = await _run(ns)
    assert out["code"] == "challenge"


async def test_a_popup_after_pay_is_a_challenge(vault):
    ns = checkout()
    ns.els["pay"].on_click = lambda: [fn(Page("about:blank")) for fn in ns.pctx.handlers["page"]]
    out, _ = await _run(ns)
    assert out["code"] == "challenge"


async def test_the_browser_closing_after_pay_is_outcome_unknown(vault):
    ns = checkout()
    hooks = Hooks()

    def gone():
        hooks.up = False

    ns.els["pay"].on_click = gone
    out, _ = await _run(ns, hooks=hooks)
    assert out["code"] == "outcome_unknown"
    assert "do NOT retry" in out["status"]


async def test_a_click_that_raises_still_counts_as_paid(vault):
    ns = checkout()

    def boom():
        raise RuntimeError(PAN)

    ns.els["pay"].on_click = boom
    out, _ = await _run(ns)
    # Never "not paid", and never "Pay was clicked": it is not known whether it landed.
    assert out["code"] == "pay_click_unknown"
    assert "do NOT retry" in out["status"] and not out["status"].startswith("Pay was clicked")
    _sweep(out)


async def test_a_cancel_after_the_claim_is_logged_without_values(vault, logs):
    # The caller cancels while Pay is being clicked: the card is spent and Pay
    # may have landed, so it must not vanish silently.
    ns = checkout()
    clicking = asyncio.Event()

    async def hang(self, timeout=None):
        clicking.set()
        await asyncio.sleep(3600)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Handle, "click", hang)
        task = asyncio.ensure_future(_run(ns))
        await asyncio.wait_for(clicking.wait(), 30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    line = [x for x in logs if "cancelled by its caller after the claim" in x]
    assert line and "paid=True" in line[0]
    _sweep(logs)


async def test_a_click_that_raised_but_moved_the_checkout_on_landed(vault):
    ns = checkout()

    def lands_then_raises():
        ns.ppage.navigate("https://shop.example.com/done")
        raise RuntimeError("detached")

    ns.els["pay"].on_click = lands_then_raises
    out, _ = await _run(ns)
    assert out["code"] == "submitted_unverified"


async def test_a_clean_click_with_no_settle_is_still_a_challenge(vault):
    ns = checkout()
    ns.els["pay"].on_click = lambda: None  # clicked, nothing moved
    out, _ = await _run(ns)
    assert out["code"] == "challenge"


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (
            lambda ns: setattr(ns.ppage.main_frame, "text", "Order total $119.99"),
            "amount_not_on_page",
        ),
        (lambda ns: ns.els["pay"].pay.update(get=True), "pay_get_form"),
        (lambda ns: ns.els["pay"].pay.update(button=False), "pay_not_button"),
        (
            lambda ns: setattr(ns.ppage, "goto_to", "https://evil.example.org/pay"),
            "checkout_redirected",
        ),
        (lambda ns: setattr(ns.ppage.main_frame, "masked", False), "checkout_load_failed"),
        (lambda ns: ns.els["number"].kinds.clear(), "field_mismatch:number"),
        (lambda ns: ns.ppage.main_frame.elements.pop("#pay"), "pay_not_found"),
        (lambda ns: ns.main_page.main_frame.elements.pop("iframe#card"), "frame_not_found:number"),
        (lambda ns: setattr(ns.els["email"], "extra", "card"), "extra_field_is_card_field"),
        # An element that lives in another frame than the one its origin is read from.
        (lambda ns: setattr(ns.els["number"], "owner", Frame(CARDS)), "field_origin:number"),
        (lambda ns: setattr(ns.els["pay"], "owner", Frame(SHOP)), "field_origin:pay"),
        (
            lambda ns: setattr(
                ns.ppage.main_frame.elements["iframe#card"][0], "owner", Frame(SHOP)
            ),
            "frame_not_found:number",
        ),
        (lambda ns: setattr(ns.els["email"], "owner", Frame(SHOP)), "extra_field_not_found"),
        # A host that is itself a public suffix: its "site" would take every tenant's storage.
        (
            lambda ns: setattr(ns.main_page, "url", "https://github.io/pay"),
            "checkout_on_public_suffix",
        ),
    ],
)
async def test_a_failed_check_before_the_tap_requests_nothing(vault, setup, code):
    ns = checkout()
    setup(ns)
    out, _ = await _run(ns)
    assert out["code"] == code
    assert vault.requests == []
    assert ns.els["number"].value == ""
    assert not pc.in_flight()


async def test_an_extra_field_that_is_a_card_field_element_is_refused(vault):
    ns = checkout()
    ns.ppage.main_frame.elements["#email"] = ns.ppage.main_frame.elements["#name"]
    out, _ = await _run(ns)
    assert out["code"] == "extra_field_is_card_field"


async def test_a_bot_check_is_refused(vault):
    ns = checkout()
    hooks = Hooks()
    hooks.interstitial.return_value = True
    out, _ = await _run(ns, hooks=hooks)
    assert out["code"] == "bot_check" and vault.requests == []


@pytest.mark.parametrize(
    ("state", "code"), [("refused", "not_approved"), ("expired", "release_expired")]
)
async def test_no_grant_fills_nothing(vault, state, code):
    ns = checkout()
    vault.statuses = [{"ok": True, "state": state, "expires_in_s": 0}]
    out, _ = await _run(ns)
    assert out["code"] == code
    assert vault.ops("release_claim") == [] and ns.els["number"].value == ""


async def test_a_vault_refusal_passes_only_a_fixed_reason(vault):
    ns = checkout()
    vault.request_reply = {"ok": False, "error": "no such active card"}
    out, _ = await _run(ns)
    assert out == cf.result("release_refused", "shop.example.com", reason="no such active card")
    vault.request_reply = {"ok": False, "error": f"odd {PAN}"}
    out, _ = await _run(ns)
    assert "reason" not in out
    vault.request_reply = {"ok": False, "error": "the approval text was refused"}
    out, _ = await _run(ns)
    assert out["code"] == "approval_text_refused"


async def test_a_page_that_moved_while_waiting_cancels_the_release(vault):
    ns = checkout()
    vault.on_status = lambda: setattr(ns.ppage.extra[0], "url", "https://evil.example.org/f")
    out, _ = await _run(ns)
    assert out["code"] == "page_changed"
    assert vault.ops("release_claim") == [] and len(vault.ops("release_cancel")) == 1


async def test_the_payment_window_closing_while_waiting_cancels(vault):
    ns = checkout()
    vault.statuses = [{"ok": True, "state": "pending", "expires_in_s": 100}]
    vault.on_status = lambda: setattr(ns.ppage, "_closed", True)
    out, _ = await _run(ns)
    assert out["code"] == "checkout_closed" and len(vault.ops("release_cancel")) == 1


async def test_a_page_that_moved_during_the_claim_types_nothing(vault):
    ns = checkout()
    vault.on_claim = lambda: setattr(ns.ppage, "url", "https://evil.example.org/x")
    out, _ = await _run(ns)
    assert out["code"] == "claimed_not_filled"
    assert ns.els["number"].value == "" and vault.ops("release_cancel") == []


@pytest.mark.parametrize("change", ["fewer", "more"])
async def test_a_reply_with_other_fields_than_tapped_types_nothing(vault, change):
    ns = checkout()

    def reply():
        card = vault.card()["card"]
        if change == "fewer":
            card.pop("cvc")
        else:
            card["postal_code"] = POSTAL
        vault.claim_reply = {"ok": True, "card": card}

    vault.on_status = reply
    out, _ = await _run(ns)
    assert out["code"] == "claimed_not_filled" and ns.els["number"].value == ""


async def test_a_value_that_does_not_land_stops_before_pay(vault, logs):
    ns = checkout()
    ns.els["cvc"].mangle = True
    clicked = []
    ns.els["pay"].on_click = lambda: clicked.append(1)
    out, _ = await _run(ns)
    assert out["code"] == "failed_before_pay"
    assert out["field"] == "cvc" and out["filled"] == ["number", "expiry", "cvc"]
    assert clicked == [] and ns.pctx.closed
    _sweep(out, logs)


async def test_pay_disabled_after_filling_stops_before_pay(vault):
    ns = checkout()
    vault.on_claim = lambda: ns.els["pay"].pay.update(live=False)
    out, _ = await _run(ns)
    assert out["code"] == "failed_before_pay" and "field" not in out


async def test_an_unanswered_claim_is_not_cancelled(vault):
    ns = checkout()

    def stop():
        vault.server.close()
        raise ConnectionError

    vault.on_claim = stop
    out, _ = await _run(ns)
    assert out["code"] == "claim_unanswered"
    assert vault.ops("release_cancel") == []


async def test_a_refused_claim_carries_the_reason(vault):
    ns = checkout()
    vault.claim_reply = {"ok": False, "error": "requester gone"}
    out, _ = await _run(ns)
    assert out["code"] == "claim_refused" and out["reason"] == "requester gone"


async def test_one_payment_at_a_time_and_turnstile_excluded(vault):
    ns = checkout()
    gate = asyncio.Event()
    vault.statuses = [{"ok": True, "state": "pending", "expires_in_s": 100}]
    vault.on_status = gate.set
    task = asyncio.ensure_future(_run(ns))
    await gate.wait()
    assert pc.in_flight() and not pc.past_claim()
    out, _ = await _run(checkout())
    assert out["code"] == "payment_in_flight"
    with pc.try_x_input() as ok:
        assert ok is False
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not pc.in_flight()
    with pc.try_x_input() as ok:
        assert ok is True
        out, _ = await _run(checkout())
        assert out["code"] == "turnstile_in_progress"


async def test_a_cancelled_call_still_cancels_the_release_and_closes(vault):
    ns = checkout()
    gate = asyncio.Event()
    vault.statuses = [{"ok": True, "state": "pending", "expires_in_s": 100}]
    vault.on_status = gate.set
    task = asyncio.ensure_future(_run(ns))
    await gate.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(vault.ops("release_cancel")) == 1 and ns.pctx.closed


async def test_abort_ends_the_flow_with_a_result_and_cancels(vault):
    ns = checkout()
    gate = asyncio.Event()
    vault.statuses = [{"ok": True, "state": "pending", "expires_in_s": 100}]
    vault.on_status = gate.set
    task = asyncio.ensure_future(_run(ns))
    await gate.wait()
    await pc.abort("mcp_exit")
    out, _ = await task
    assert out["code"] == "browser_closed"
    assert len(vault.ops("release_cancel")) == 1 and ns.pctx.closed and not pc.in_flight()


@pytest.mark.parametrize(
    ("hook", "change", "code"),
    [
        ("on_status", lambda ns: ns.els["pay"].pay.update(label="Pay $99.99"), "page_changed"),
        (
            "on_status",
            lambda ns: setattr(ns.ppage.main_frame, "text", "Total $29.99"),
            "page_changed",
        ),
        (
            "on_claim",
            lambda ns: setattr(ns.ppage.main_frame, "text", "Total $29.99"),
            "claimed_not_filled",
        ),
        # A child frame whose mask cannot be put back.
        (
            "on_claim",
            lambda ns: (
                setattr(ns.ppage.extra[0], "masked", False),
                setattr(ns.ppage.extra[0], "repairable", False),
            ),
            "claimed_not_filled",
        ),
        # Same origin, amount, Pay and fields, but another order under the tapped title.
        ("on_status", lambda ns: setattr(ns.ppage, "_title", "Order 2 - Checkout"), "page_changed"),
        (
            "on_claim",
            lambda ns: setattr(ns.ppage, "_title", "Order 2 - Checkout"),
            "claimed_not_filled",
        ),
    ],
)
async def test_each_check_after_the_tap_holds(vault, hook, change, code):
    ns = checkout()
    clicked = []
    ns.els["pay"].on_click = lambda: clicked.append(1)
    setattr(vault, hook, lambda: change(ns))
    out, _ = await _run(ns)
    assert out["code"] == code and clicked == []


async def test_a_child_frame_the_init_script_missed_is_masked_and_rechecked(vault):
    # Camoufox's init script misses some nested helper frames: the check masks
    # such a frame itself, then re-checks it, and the payment goes on.
    ns = checkout()
    helper = Frame("https://js.cards.example.net/helper")
    helper.masked, helper.mask_absent = False, True
    helper.parent_frame = ns.ppage.extra[0]
    ns.ppage.extra.append(helper)
    out, _ = await _run(ns)
    assert out["code"] == "submitted_unverified"
    assert helper.injected >= 1 and helper.masked is True


async def test_a_frame_still_navigating_at_load_is_asked_again(vault):
    # A new iframe leaving about:blank cannot answer for a moment: the check
    # asks again instead of refusing the checkout (measured: 16% of loads).
    ns = checkout()
    ns.ppage.extra[0].mask_raises = 2
    out, _ = await _run(ns)
    assert out["code"] == "submitted_unverified"


async def test_a_frame_that_never_answers_fails_the_check(monkeypatch):
    monkeypatch.setattr(pc, "MASK_SETTLE_S", 0.05)
    page = Page(SHOP, frames=[Frame(CARDS)])
    page.extra[0].mask_raises = 10**6
    assert await pc._mask_ok(page) is False


async def test_a_frame_that_answers_unmasked_fails_at_once(monkeypatch):
    # Only an unanswerable frame is asked again; an unmasked answer is final.
    monkeypatch.setattr(pc, "MASK_SETTLE_S", 30.0)
    page = Page(SHOP)
    page.main_frame.masked = False
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    assert await pc._mask_ok(page) is False
    assert loop.time() - t0 < 5.0


async def test_the_top_document_is_never_repaired(vault):
    ns = checkout()
    # No mask at all and repairable, but the top document must pass on its own.
    ns.ppage.main_frame.masked, ns.ppage.main_frame.mask_absent = False, True
    out, _ = await _run(ns)
    assert out["code"] == "checkout_load_failed"
    assert ns.ppage.main_frame.injected == 0
    assert vault.ops("release_request") == []


async def test_a_child_frame_whose_mask_was_defeated_is_not_repaired(vault):
    # The mask is there but no longer holds (a page script beat the observer):
    # a tamper signal, never papered over.
    ns = checkout()
    ns.ppage.extra[0].masked = False  # present (mask_absent stays False), not holding
    out, _ = await _run(ns)
    assert out["code"] == "checkout_load_failed"
    assert ns.ppage.extra[0].injected == 0
    assert vault.ops("release_request") == []


async def test_a_child_frame_that_stays_unmasked_fails_closed(vault):
    ns = checkout()
    ns.ppage.extra[0].masked, ns.ppage.extra[0].mask_absent = False, True
    ns.ppage.extra[0].repairable = False
    out, _ = await _run(ns)
    assert out["code"] == "checkout_load_failed"
    assert vault.ops("release_request") == []


async def test_the_mask_is_checked_before_each_field(vault):
    ns = checkout()

    real = Handle.fill

    async def fill(self, value, timeout=None):
        await real(self, value, timeout)
        if self.el is ns.els["number"]:
            ns.ppage.main_frame.masked = False

    Handle.fill = fill
    try:
        out, _ = await _run(ns)
    finally:
        Handle.fill = real
    assert out["code"] == "failed_before_pay" and out["field"] == "expiry"
    assert ns.els["expiry"].value == ""


async def test_a_frame_moved_before_pay_stops_before_pay(vault):
    ns = checkout()
    real = Handle.fill

    async def fill(self, value, timeout=None):
        await real(self, value, timeout)
        if self.el is ns.els["name"]:  # the last field: only the Pay check is left
            ns.ppage.extra[0].url = "https://evil.example.org/f"

    Handle.fill = fill
    try:
        out, _ = await _run(ns)
    finally:
        Handle.fill = real
    assert out["code"] == "failed_before_pay" and "field" not in out


async def test_a_challenge_like_frame_present_before_pay_is_ignored(vault):
    ns = checkout()
    ns.ppage.extra.append(Frame("https://hooks.stripe.com/widget"))
    out, _ = await _run(ns)
    assert out["code"] == "submitted_unverified"


async def test_a_preloaded_bank_frame_revealed_after_pay_is_a_challenge(vault):
    # Loaded hidden before Pay, shown after it, while the checkout moved on in
    # place: its URL was already seen, so only its visibility tells.
    ns = checkout()
    bank = Frame("https://hooks.stripe.com/3d_secure_2/x")
    bank.parent_frame = ns.ppage.main_frame
    ns.ppage.extra.append(bank)

    def reveal():
        bank.visible = True
        ns.ppage.navigate("https://shop.example.com/processing")

    ns.els["pay"].on_click = reveal
    out, _ = await _run(ns)
    assert out["code"] == "challenge"


async def test_a_flag_set_by_the_last_read_back_stops_before_pay(vault, logs):
    # The route sees the number while the very last field is read back, after
    # every earlier flag check: Pay must not be clicked.
    ns = checkout()
    clicked: list = []
    ns.els["pay"].on_click = lambda: clicked.append(1)
    real = Handle.input_value

    async def input_value(self, timeout=None):
        flow = pc._flow
        if self.el is ns.els["name"] and flow is not None and len(flow.typed) == len(ns.fields):
            await ns.pctx.route_handler(Route(f"https://t.example.org/c?n={PAN}"))
        return await real(self, timeout)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Handle, "input_value", input_value)
        out, _ = await _run(ns)
    assert out["code"] == "card_url_before_pay" and clicked == []
    _sweep(out, logs)


@pytest.mark.parametrize("how", ["navigate", "navigate_away", "popup"])
async def test_a_checkout_that_moves_on_while_typing_is_an_unknown_outcome(vault, logs, how):
    # The last field's input handler submits the checkout itself: Pay is not
    # clicked, and the result must not say nothing was paid. Landing on another
    # origin fails an earlier check first; that stop is reported as the move too.
    ns = checkout()
    clicked: list = []
    ns.els["pay"].on_click = lambda: clicked.append(1)

    def submit():
        if how == "navigate":
            ns.ppage.navigate("https://shop.example.com/done")
        elif how == "navigate_away":
            ns.ppage.navigate("https://pay.provider.example/done")
        else:
            for fn in ns.pctx.handlers["page"]:
                fn(Page("about:blank"))

    out = await _filling(ns, "name", submit)
    assert out["code"] == "moved_while_filling" and clicked == []
    assert "do NOT retry" in out["status"] and "NOT clicked" in out["status"]
    _sweep(out, logs)


@pytest.mark.parametrize("path", ["#card", "/step-2"])
async def test_a_same_document_url_change_while_typing_is_not_a_move(vault, path):
    # A page marking its step (a fragment, pushState) sends no request: it pays.
    ns = checkout()
    out = await _filling(ns, "number", lambda: ns.ppage.same_document(SHOP + path))
    assert out["code"] == "submitted_unverified"


async def test_a_submit_still_in_flight_while_typing_is_an_unknown_outcome(vault, logs):
    # The input handler posts the form and disables Pay; nothing has committed.
    ns = checkout()
    clicked: list = []
    ns.els["pay"].on_click = lambda: clicked.append(1)

    def submit():
        ns.ppage.request_navigation("https://shop.example.com/pay")
        ns.els["pay"].pay.update(live=False)

    out = await _filling(ns, "name", submit)
    assert out["code"] == "moved_while_filling" and clicked == []
    _sweep(out, logs)


async def test_a_card_url_while_moving_on_is_still_reported_as_exposure(vault, logs):
    # Both at once: the exposure is never hidden behind the move.
    ns = checkout()
    clicked: list = []
    ns.els["pay"].on_click = lambda: clicked.append(1)
    real = Handle.fill

    async def fill(self, value, timeout=None):
        await real(self, value, timeout)
        if self.el is ns.els["name"]:
            await ns.pctx.route_handler(Route(f"https://t.example.org/c?n={PAN}"))
            ns.ppage.navigate("https://pay.provider.example/done")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Handle, "fill", fill)
        out, _ = await _run(ns)
    assert out["code"] == "card_url_before_pay" and clicked == []
    assert "exposed" in out["status"] and "do NOT retry" in out["status"]
    _sweep(out, logs)


async def test_a_flow_ended_while_typing_after_a_move_is_an_unknown_outcome(vault):
    # The browser goes away right after the checkout moved on mid-typing.
    ns = checkout()
    typing = asyncio.Event()
    real = Handle.fill

    async def fill(self, value, timeout=None):
        await real(self, value, timeout)
        if self.el is ns.els["number"]:
            ns.ppage.navigate("https://shop.example.com/done")
            typing.set()
            await asyncio.sleep(3600)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Handle, "fill", fill)
        task = asyncio.ensure_future(_run(ns))
        await asyncio.wait_for(typing.wait(), 30)
        await pc.abort("mcp_exit")
        out, _ = await task
    assert out["code"] == "moved_while_filling"


async def test_a_second_popup_while_typing_is_a_move(vault):
    # One popup opened during the claim, before typing: a new one while the
    # card is typed still counts.
    ns = checkout()
    clicked: list = []
    ns.els["pay"].on_click = lambda: clicked.append(1)

    def popup():
        for fn in ns.pctx.handlers["page"]:
            fn(Page("about:blank"))

    vault.on_claim = popup
    out = await _filling(ns, "name", popup)
    assert out["code"] == "moved_while_filling" and clicked == []


async def test_a_select_matched_by_its_label_reads_back_as_landed(vault):
    # Options whose values are not the month or year itself: chosen by label,
    # so the read-back must accept the selected option's label.
    ns = checkout(select_expiry=True)
    for kind, shown in (
        ("exp_month", [str(m) for m in range(1, 13)]),
        ("exp_year", [str(YEAR + i) for i in range(5)]),
    ):
        el = ns.els[kind]
        el.options = [f"opt-{v}" for v in shown]
        el.labels = {f"opt-{v}": v for v in shown}
    out, _ = await _run(ns)
    assert out["code"] == "submitted_unverified"
    assert ns.els["exp_month"].value == "opt-12" and ns.els["exp_year"].value == f"opt-{YEAR}"


async def test_a_select_whose_label_is_wrong_does_not_land(vault):
    ns = checkout(select_expiry=True)
    el = ns.els["exp_month"]
    el.options = [f"opt-{m}" for m in range(1, 13)]
    el.labels = {f"opt-{m}": str(m) for m in range(1, 13)}
    real = Handle.select_option

    async def select_option(self, timeout=None, value=None, label=None):
        await real(self, timeout, value, label)
        if self.el is el:
            self.el.value = "opt-3"  # the page picks another month

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Handle, "select_option", select_option)
        out, _ = await _run(ns)
    assert out["code"] == "failed_before_pay" and out["field"] == "exp_month"


async def _filling(ns, last_kind, change):
    """Run with ``change`` applied right after ``last_kind`` is typed."""
    real = Handle.fill

    async def fill(self, value, timeout=None):
        await real(self, value, timeout)
        if self.el is ns.els[last_kind]:
            change()

    Handle.fill = fill
    try:
        return (await _run(ns))[0]
    finally:
        Handle.fill = real


@pytest.mark.parametrize(
    "change",
    [
        lambda ns: ns.els["pay"].pay.update(label="Pay $999.99"),
        lambda ns: ns.els["pay"].pay.update(button=False),
        lambda ns: setattr(ns.els["email"], "value", "other@example.org"),
        lambda ns: setattr(ns.ppage.main_frame, "text", "Total $29.99"),
        lambda ns: setattr(ns.ppage.extra[0], "url", "https://evil.example.org/f"),
        lambda ns: setattr(ns.ppage, "_title", "Order 2 - Checkout"),
    ],
)
async def test_pay_and_extras_are_rechecked_right_before_pay(vault, change):
    ns = checkout()
    clicked = []
    ns.els["pay"].on_click = lambda: clicked.append(1)
    out = await _filling(ns, "name", lambda: change(ns))
    assert out["code"] == "failed_before_pay" and clicked == []


@pytest.mark.parametrize(
    ("kind", "value"),
    [("number", ""), ("number", "4000056655665556"), ("cvc", ""), ("expiry", "01/30")],
)
async def test_a_card_field_rewritten_by_a_later_fill_stops_before_pay(vault, logs, kind, value):
    """Each field passed its own read-back; filling the last one cleared or
    rewrote an earlier one. Every card field is read back again before Pay."""
    ns = checkout()
    clicked = []
    ns.els["pay"].on_click = lambda: clicked.append(1)
    out = await _filling(ns, "name", lambda: setattr(ns.els[kind], "value", value))
    assert out["code"] == "failed_before_pay" and out["field"] == kind and clicked == []
    assert out["filled"] == ["number", "expiry", "cvc", "name"]
    _sweep(out, logs)


async def test_card_fields_are_read_back_after_the_last_pay_check(vault):
    """A script that blanks the CVC while the final pre-Pay check reads the
    button: the card fields are read back after it."""
    ns = checkout()
    clicked = []
    ns.els["pay"].on_click = lambda: clicked.append(1)
    typed, reads = [], []
    real_fill, real_eval = Handle.fill, Handle.evaluate

    async def fill(self, value, timeout=None):
        await real_fill(self, value, timeout)
        if self.el is ns.els["name"]:
            typed.append(1)

    async def evaluate(self, js, arg=None):
        if js is pc.PAY_JS and typed:
            reads.append(1)
            if len(reads) == 1:  # the final _same_page's read
                ns.els["cvc"].value = ""
        return await real_eval(self, js, arg)

    Handle.fill, Handle.evaluate = fill, evaluate
    try:
        out, _ = await _run(ns)
    finally:
        Handle.fill, Handle.evaluate = real_fill, real_eval
    assert len(reads) == 1
    assert out["code"] == "failed_before_pay" and out["field"] == "cvc" and clicked == []


async def test_a_widget_that_mounts_after_load_is_waited_for_before_the_tap(vault):
    """Hosted card frames and Pay often mount after DOMContentLoaded."""
    ns = checkout()
    for page in (ns.main_page, ns.ppage):
        page.main_frame.late["iframe#card"] = page.main_frame.elements.pop("iframe#card")
    ns.ppage.main_frame.late["#pay"] = ns.ppage.main_frame.elements.pop("#pay")
    out, _ = await _run(ns)
    assert out["code"] == "submitted_unverified"
    assert "iframe#card" in ns.main_page.main_frame.waits  # the handover walk waited too
    assert {"iframe#card", "#pay"} <= set(ns.ppage.main_frame.waits)


async def test_after_the_tap_an_absent_element_is_not_waited_for(vault):
    """Past the tap, absence means the page changed: no wait, release cancelled."""
    ns = checkout()

    def unmount():
        ns.ppage.main_frame.late["#pay"] = ns.ppage.main_frame.elements.pop("#pay")

    vault.on_status = unmount
    out, _ = await _run(ns)
    assert out["code"] == "pay_not_found"
    assert vault.ops("release_claim") == [] and vault.ops("release_cancel")


async def test_two_card_kinds_on_one_element_are_refused_before_the_tap(vault):
    ns = checkout()
    ns.els["number"].kinds.add("cvc")  # malformed markup: one input reads as both
    ns.fields["cvc"] = {"selector": "#num", "frames": ["iframe#card"]}
    out, _ = await _run(ns)
    assert out["code"] == "field_ambiguous:cvc" and vault.requests == []


@pytest.mark.parametrize(
    ("hits", "code"),
    [
        (1, "page_changed"),  # after the grant: the release is cancelled
        (2, "claimed_not_filled"),  # after the claim: nothing typed
        (3, "failed_before_pay"),  # before Pay: not clicked
    ],
)
async def test_a_bot_check_appearing_after_the_tap_stops_the_flow(vault, hits, code):
    ns = checkout()
    clicked = []
    ns.els["pay"].on_click = lambda: clicked.append(1)
    hooks = Hooks()
    # The first call is the pre-tap check; the next ones are _same_page's.
    hooks.interstitial = AsyncMock(side_effect=[False] * hits + [True] * 10)
    out, _ = await _run(ns, hooks=hooks)
    assert out["code"] == code and clicked == []
    if code == "page_changed":
        assert vault.ops("release_claim") == []


async def test_every_frame_on_a_chain_gets_its_storage(vault):
    """A provider wrapper frame around the secure-input frame: storage of both
    origins is handed over, not only the innermost one's."""
    ns = checkout()
    for page in (ns.main_page, ns.ppage):
        card = page.extra[0]
        wrap = Frame("https://wrap.example.org/w", {"iframe#card": [El(tag="iframe", child=card)]})
        page.main_frame.elements["iframe#wrap"] = [El(tag="iframe", child=wrap)]
        page.extra.append(wrap)
    for kind in ("number", "expiry", "cvc"):
        ns.fields[kind] = {**ns.fields[kind], "frames": ["iframe#wrap", "iframe#card"]}
    ns.main.state["cookies"].append({"name": "w", "domain": "wrap.example.org", "value": "c4"})
    ns.main.state["origins"].append(
        {"origin": "https://wrap.example.org", "localStorage": [{"name": "k", "value": "v"}]}
    )
    out, _ = await _run(ns)
    assert out["code"] == "submitted_unverified"
    handed = ns.main.new_context_kwargs["storage_state"]
    assert sorted(c["name"] for c in handed["cookies"]) == ["cart", "m", "w"]
    assert sorted(o["origin"] for o in handed["origins"]) == [
        "https://shop.example.com",
        "https://wrap.example.org",
    ]


async def test_an_extra_field_changed_while_waiting_cancels(vault):
    ns = checkout()
    vault.on_status = lambda: setattr(ns.els["email"], "value", "")
    out, _ = await _run(ns)
    assert out["code"] == "page_changed" and vault.ops("release_claim") == []


async def test_an_extra_field_that_does_not_keep_its_value_requests_nothing(vault):
    ns = checkout()
    ns.els["email"].mangle = True
    out, _ = await _run(ns)
    assert out["code"] == "extra_field_failed" and vault.requests == []


async def test_two_extra_names_on_one_element_are_refused(vault):
    ns = checkout()
    out, _ = await _run(
        ns,
        extra_fields={
            "email": {"selector": "#email", "value": EMAIL},
            "company": {"selector": "#email", "value": "Acme"},
        },
    )
    assert out["code"] == "extra_field_not_found" and vault.requests == []


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (lambda ns: setattr(ns.ppage, "goto_to", "http://shop.example.com/checkout"), "not_https"),
        (
            lambda ns: setattr(ns.ppage.extra[0], "url", "http://js.cards.example.net/f"),
            "field_origin:number",
        ),
        (lambda ns: setattr(ns.main, "browser", None), "checkout_load_failed"),
    ],
)
async def test_plain_http_and_no_browser_are_refused(vault, setup, code):
    ns = checkout()
    setup(ns)
    out, _ = await _run(ns)
    assert out["code"] == code and vault.requests == []
    assert ns.els["number"].value == ""
    if code == "checkout_load_failed":  # refused before anything is read or created
        assert not getattr(ns.main, "exported", False)


async def test_a_merchant_hosted_bank_check_is_a_challenge(vault):
    ns = checkout()
    ns.els["pay"].on_click = lambda: ns.ppage.navigate("https://shop.example.com/pay/3ds/start")
    out, _ = await _run(ns)
    assert out["code"] == "challenge"


async def test_session_storage_is_handed_over_for_the_merchant_origin(vault):
    ns = checkout()
    ns.main_page.main_frame.session = {"cart": "c-1"}
    out, _ = await _run(ns)
    assert out["code"] == "submitted_unverified"
    scripts = [e[1] for e in ns.log if e[0] == "init_script"]
    assert scripts[0] == pc.MASK_INIT_JS and len(scripts) == 2
    assert '{"cart": "c-1"}' in scripts[1] and json.dumps(SHOP) in scripts[1]
    assert ("dispose", scripts[1]) in ns.log  # seeds the first document only
    assert ("dispose", scripts[0]) not in ns.log


async def test_progress_increases(monkeypatch):
    clock = [0.0]

    async def impl(*_a, report=None, **_k):
        for _ in range(3):
            clock[0] += 10
            await report("waiting")
        return {"code": "x"}

    sent = []
    ctx = MagicMock()
    ctx.report_progress = AsyncMock(side_effect=lambda p, message=None: sent.append(p))
    monkeypatch.setattr(browser, "_impl_browser_fill_card", impl)
    monkeypatch.setattr(browser, "time", MagicMock(monotonic=lambda: clock[0]))
    fn = getattr(browser.browser_fill_card, "fn", browser.browser_fill_card)
    await fn(CARD_REF, "$1", {"number": "#n"}, "#p", ctx=ctx)
    assert sent == [1, 2, 3]


async def test_abort_after_the_pay_click_is_outcome_unknown(vault):
    ns = checkout()
    ns.els["pay"].on_click = lambda: asyncio.get_running_loop().call_soon(
        lambda: asyncio.ensure_future(pc.abort("mcp_exit"))
    )
    out, _ = await _run(ns)
    assert out["code"] == "outcome_unknown" and ns.pctx.closed


async def test_a_context_that_will_not_close_closes_the_browser_first(vault):
    ns = checkout()
    ns.pctx.close_fails = True
    hooks = Hooks()
    hooks.close_browser.side_effect = lambda: setattr(hooks, "up", False)
    out, _ = await _run(ns, hooks=hooks)
    assert out["code"] == "submitted_unverified"
    hooks.close_browser.assert_awaited_once()  # awaited before the guard is released
    assert not pc.in_flight()


@pytest.mark.parametrize("fails", [False, True])
async def test_a_browser_that_will_not_close_keeps_payments_refused_while_it_lives(vault, fails):
    ns = checkout()
    ns.pctx.close_fails = True
    hooks = Hooks()
    if fails:
        hooks.close_browser.side_effect = RuntimeError("lock busy")
    out, _ = await _run(ns, hooks=hooks)  # the close does not detach the browser
    for _ in range(3):
        await asyncio.sleep(0)
    assert hooks.close_browser.await_count == 2  # tried again in the background
    assert pc.in_flight()
    with pc.try_x_input() as ok:
        assert ok is False
    again, _ = await _run(checkout())
    assert again["code"] == "payment_in_flight"
    hooks.up = False  # that browser is gone: the guard retires with it
    assert not pc.in_flight()


async def test_the_main_browser_restarting_before_the_claim_cancels(vault):
    ns = checkout()
    hooks = Hooks()
    vault.statuses = [{"ok": True, "state": "pending", "expires_in_s": 100}]
    vault.on_status = lambda: setattr(hooks, "up", False)
    out, _ = await _run(ns, hooks=hooks)
    assert out["code"] == "browser_closed" and len(vault.ops("release_cancel")) == 1
    hooks.close_browser.assert_not_awaited()


async def test_past_claim_holds_until_teardown_ends(vault):
    ns = checkout()
    seen = []
    ns.els["pay"].on_click = lambda: seen.append((pc.in_flight(), pc.past_claim()))
    await _run(ns)
    assert seen == [(True, True)] and not pc.past_claim()


async def test_is_payment_page_only_for_the_payment_context(vault):
    ns = checkout()
    seen = []
    ns.els["pay"].on_click = lambda: seen.append(
        (pc.is_payment_page(ns.ppage), pc.is_payment_page(ns.main_page))
    )
    await _run(ns)
    assert seen == [(True, False)]
    assert not pc.is_payment_page(ns.ppage)  # torn down


# ---------------------------------------------------------------------------
# browser.py: the entry point and the lifecycle hooks
# ---------------------------------------------------------------------------


@pytest.fixture
def camoufox(monkeypatch):
    page = MagicMock()
    page.url = SHOP
    page.is_closed.return_value = False
    ctx = MagicMock()
    monkeypatch.setattr(browser, "_active_page", page)
    monkeypatch.setattr(browser, "_stealth_page", page)
    monkeypatch.setattr(browser, "_stealth_browser", ctx)
    monkeypatch.setattr(browser, "_browser_lock", asyncio.Lock())
    monkeypatch.setattr(browser, "_layer_last_used", {})
    monkeypatch.setattr(
        browser, "_layer_of", lambda p: BrowserLayer.CAMOUFOX if p is page else None
    )
    monkeypatch.setattr(cf, "card_fill_enabled", lambda: True)
    monkeypatch.delenv("DEBUG", raising=False)
    return page, ctx


def _impl_args(**over):
    kw = dict(card_ref=CARD_REF, amount="$1", fields={"number": "#n"}, pay="#p")
    kw.update(over)
    return kw


@pytest.mark.parametrize(
    ("setup", "code"),
    [
        (lambda m: m.setattr(cf, "card_fill_enabled", lambda: False), "card_fill_disabled"),
        (lambda m: m.setenv("DEBUG", "pw:*"), "playwright_debug_on"),
        (lambda m: m.setattr(browser, "_active_page", None), "no_page"),
        (
            lambda m: m.setattr(browser, "_layer_of", lambda p: BrowserLayer.CHROMIUM),
            "not_camoufox",
        ),
        (lambda m: m.setattr(pc, "_prefs_pinned", False), "prefs_not_pinned"),
    ],
)
async def test_impl_prechecks(camoufox, monkeypatch, setup, code):
    run = AsyncMock()
    monkeypatch.setattr(pc, "fill_card", run)
    setup(monkeypatch)
    out = await browser._impl_browser_fill_card(**_impl_args())
    assert out["code"] == code
    run.assert_not_awaited()


async def test_impl_refuses_bad_arguments_and_hands_over_the_main_page(camoufox, monkeypatch):
    page, ctx = camoufox
    run = AsyncMock(return_value={"code": "x"})
    monkeypatch.setattr(pc, "fill_card", run)
    assert (await browser._impl_browser_fill_card(**_impl_args(pay=None)))["code"] == "bad_pay"
    assert (await browser._impl_browser_fill_card(**_impl_args()))["code"] == "x"
    _args, main_ctx, main_page, hooks, _report = run.await_args.args
    assert main_ctx is ctx and main_page is page and hooks.alive()
    monkeypatch.setattr(browser, "_stealth_browser", MagicMock())
    assert not hooks.alive()


async def test_impl_turns_an_exception_into_fixed_text(camoufox, monkeypatch):
    monkeypatch.setattr(pc, "fill_card", AsyncMock(side_effect=RuntimeError(PAN)))
    out = await browser._impl_browser_fill_card(**_impl_args())
    assert out["code"] == "internal_error"
    _sweep(out)


async def test_a_stale_restart_is_refused_past_the_claim(camoufox, monkeypatch):
    page, _ctx = camoufox
    page.is_closed.return_value = True
    cleanup = AsyncMock()
    monkeypatch.setattr(browser, "_cleanup_camoufox", cleanup)
    monkeypatch.setattr(pc, "_flow", pc._Flow(task=None, claimed=True))
    with pytest.raises(pc.PaymentInFlight):
        await browser._ensure_browser()
    cleanup.assert_not_awaited()
    monkeypatch.setattr(browser, "_get_page", AsyncMock(side_effect=pc.PaymentInFlight("x")))
    out = await browser._impl_browser_navigate("https://example.com")
    assert out["code"] == "payment_in_flight"


async def test_idle_reclaim_skips_camoufox_while_a_payment_runs(camoufox, monkeypatch):
    cleanup = AsyncMock()
    monkeypatch.setattr(browser, "_cleanup_camoufox", cleanup)
    monkeypatch.setattr(browser, "_layer_open", lambda layer: layer is BrowserLayer.CAMOUFOX)
    browser._layer_last_used[BrowserLayer.CAMOUFOX] = 0.0
    monkeypatch.setattr(pc, "_flow", pc._Flow(task=None))
    await browser._reclaim_idle_layers(browser._IDLE_TIMEOUT_S + 10)
    cleanup.assert_not_awaited()
    monkeypatch.setattr(pc, "_flow", None)
    await browser._reclaim_idle_layers(browser._IDLE_TIMEOUT_S + 10)
    cleanup.assert_awaited_once()


async def test_turnstile_x_clicks_stand_down_while_a_payment_runs(monkeypatch):
    click = AsyncMock(return_value=True)
    monkeypatch.setattr(browser, "_vnc_click_now", click)
    monkeypatch.setattr(pc, "_flow", pc._Flow(task=None))
    assert await browser._vnc_click_turnstile(MagicMock()) is False
    click.assert_not_awaited()
    monkeypatch.setattr(pc, "_flow", None)
    assert await browser._vnc_click_turnstile(MagicMock()) is True


async def test_get_page_refuses_a_payment_page(monkeypatch):
    page = MagicMock()
    monkeypatch.setattr(browser, "_ensure_vnc", AsyncMock())
    monkeypatch.setattr(browser, "_ensure_browser", AsyncMock(return_value=page))
    monkeypatch.setattr(browser, "_start_idle_watcher", lambda: None)
    monkeypatch.setattr(browser, "_active_page", None)
    monkeypatch.setattr(pc, "_flow", pc._Flow(task=None, pctx=page.context))
    with pytest.raises(RuntimeError):
        await browser._get_page()
    assert browser._active_page is None


async def test_the_escalation_closes_only_the_launch_the_payment_ran_on(monkeypatch):
    cleanup = AsyncMock()
    monkeypatch.setattr(browser, "_cleanup_camoufox", cleanup)
    monkeypatch.setattr(browser, "_browser_lock", asyncio.Lock())
    ctx = MagicMock()
    monkeypatch.setattr(browser, "_stealth_browser", MagicMock())  # relaunched since
    await browser._close_browser_for_payment(ctx)
    cleanup.assert_not_awaited()
    monkeypatch.setattr(browser, "_stealth_browser", ctx)
    await browser._close_browser_for_payment(ctx)
    cleanup.assert_awaited_once()


async def test_the_main_window_closing_aborts_the_payment_outside_the_lock(monkeypatch):
    ctx = MagicMock()
    seen = []

    async def abort(reason):
        seen.append((reason, browser._browser_lock.locked()))

    monkeypatch.setattr(pc, "abort", abort)
    monkeypatch.setattr(browser, "_stealth_browser", ctx)
    monkeypatch.setattr(browser, "_browser_lock", asyncio.Lock())
    monkeypatch.setattr(browser, "_pending_closes", set())
    monkeypatch.setattr(browser, "_cleanup_camoufox", AsyncMock())
    browser._on_local_context_closed(BrowserLayer.CAMOUFOX, ctx)
    await asyncio.wait(set(browser._pending_closes))
    assert seen == [("browser_closed", False)]


async def test_mcp_exit_aborts_the_payment_first(monkeypatch):
    order = []
    monkeypatch.setattr(pc, "abort", AsyncMock(side_effect=lambda r: order.append(("abort", r))))
    for name in (
        "_cleanup_remote_cdp",
        "_cleanup_tinyfish",
        "_cleanup_chromium",
        "_cleanup_camoufox",
    ):
        monkeypatch.setattr(browser, name, AsyncMock(side_effect=lambda n=name: order.append(n)))
    monkeypatch.setattr(browser, "_idle_task", None)
    monkeypatch.setattr(browser, "_pending_closes", set())
    await browser.async_cleanup()
    assert order[0] == ("abort", "mcp_exit")


async def test_a_launch_pins_card_fill_prefs_only_when_on(tmp_path, monkeypatch):
    import sys
    from unittest.mock import patch

    from genesis.browser import engine, profile_backup

    monkeypatch.setenv("DISPLAY", ":0")  # a launch sets it

    for on in (True, False):
        monkeypatch.setattr(browser, "_stealth_page", None)
        monkeypatch.setattr(browser, "_stealth_browser", None)
        monkeypatch.setattr(browser, "_stealth_cm", None)
        monkeypatch.setattr(browser, "_browser_lock", asyncio.Lock())
        monkeypatch.setattr(browser, "_PROFILE_DIR", tmp_path / "profile")
        monkeypatch.setattr(profile_backup, "backup_camoufox_profile", lambda *a: None)
        monkeypatch.setattr(cf, "card_fill_enabled", lambda on=on: on)
        page = MagicMock()
        page.is_closed.return_value = False
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=MagicMock(pages=[page]))
        ctor = MagicMock(return_value=cm)
        api = MagicMock(AsyncCamoufox=ctor)
        ready = engine.EngineStatus(engine.READY, "Camoufox", tmp_path / "bin")
        with (
            patch.object(engine, "resolve_camoufox_engine", return_value=ready),
            patch.dict(
                sys.modules, {"camoufox": MagicMock(async_api=api), "camoufox.async_api": api}
            ),
        ):
            await browser._ensure_browser()
        prefs = ctor.call_args.kwargs["firefox_user_prefs"]
        assert ("places.history.enabled" in prefs) is on
        assert prefs["browser.sessionhistory.max_entries"] == 10
        assert pc.prefs_pinned() is on


# ---------------------------------------------------------------------------
# The isolation contract
# ---------------------------------------------------------------------------

_SRC = Path(__file__).resolve().parents[2] / "src" / "genesis"
_OWNER = _SRC / "mcp" / "health" / "payment_context.py"
# CDP layers whose own browsers are walked (remote Chrome, TinyFish), by receiver name.
_CDP_RECEIVERS = {"remote_browser", "_remote_browser", "_tinyfish_browser"}
_CAMOUFOX_ROOTS = {"_stealth_browser", "_stealth_page", "_active_page", "_stealth_cm"}


def _root(node):
    while isinstance(node, (ast.Attribute, ast.Subscript, ast.Call)):
        node = node.value if not isinstance(node, ast.Call) else node.func
    return node.id if isinstance(node, ast.Name) else None


def test_only_payment_context_reaches_a_second_camoufox_context():
    """An accident guard, not a proof: no module but payment_context reads
    ``.browser`` off a Camoufox object, or touches ``.contexts`` / ``new_context``
    outside the CDP layers; and nothing reads payment_context's private state."""
    bad = []
    for path in _SRC.rglob("*.py"):
        if path == _OWNER:
            continue
        text = path.read_text()
        if not any(w in text for w in (".browser", "contexts", "new_context", "payment_context")):
            continue  # nothing the rules below look at (keeps the walk fast)
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute):
                continue
            root = _root(node.value)
            where = f"{path.relative_to(_SRC)}:{node.lineno}"
            if node.attr == "browser" and root in _CAMOUFOX_ROOTS:
                bad.append(where)
            if node.attr in ("contexts", "new_context") and root not in _CDP_RECEIVERS:
                bad.append(where)
            if root in ("payment_context", "pc") and node.attr.startswith("_"):
                bad.append(where)
    assert bad == []


def test_the_contract_test_catches_a_reach():
    tree = ast.parse("x = _stealth_browser.browser.contexts[1]")
    attrs = [n for n in ast.walk(tree) if isinstance(n, ast.Attribute)]
    assert {(n.attr, _root(n.value)) for n in attrs} == {
        ("contexts", "_stealth_browser"),
        ("browser", "_stealth_browser"),
    }


# ── round 7 ─────────────────────────────────────────────────────────────────
async def test_a_popup_before_pay_does_not_make_a_clean_pay_a_challenge(vault):
    # A window opened during the claim (before typing) is not something Pay did.
    ns = checkout()

    def popup():
        for fn in ns.pctx.handlers["page"]:
            fn(Page("about:blank"))

    vault.on_claim = popup
    out, _ = await _run(ns)
    assert out["code"] == "submitted_unverified"


async def test_a_cancel_while_the_release_is_requested_still_cancels_it(vault):
    # The vault makes the release, then the call is cancelled before the reply:
    # the nonce is still learned, so teardown cancels the pending tap.
    ns = checkout()
    vault.hold_request = asyncio.Event()
    task = asyncio.ensure_future(_run(ns))
    await vault.request_seen.wait()
    task.cancel()
    await asyncio.sleep(0.05)
    vault.hold_request.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert [r.get("nonce") for r in vault.ops("release_cancel")] == ["nonce-1"]
    assert ns.pctx.closed


# ── round 8: the network guard covers every typed value ────────────────────
YY = f"{YEAR % 100:02d}"
SHOPX = "https://shop.example.com/charge"


def _pct(text: str) -> str:
    """Every character percent-encoded (what a page or a form may do)."""
    return "".join(f"%{b:02X}" for b in text.encode())


def _wide(digits: str) -> str:
    """Full-width Unicode digits, percent-encoded as UTF-8."""
    return quote("".join(chr(0xFF10 + int(d)) for d in digits))


def _all_guard():
    return _guard(cvc=CVC, expiry=f"12/{YY}", name=NAME, postal_code=POSTAL)


@pytest.mark.parametrize(
    "url",
    [
        f"{SHOPX}?cvc={CVC}",  # a query value
        f"{SHOPX}?a=1&cvc={CVC}&b=2",
        f"{SHOPX}?ok=1#cvc={CVC}",  # a fragment parameter
        f"{SHOPX};cvc={CVC}",  # a matrix parameter
        f"https://shop.example.com/pay/{CVC}/x",  # a whole path segment
        f"{SHOPX}?cvc={_pct(CVC)}",  # encoded once
        f"{SHOPX}?cvc={quote(_pct(CVC))}",  # encoded twice
        f"{SHOPX}?cvc={_wide(CVC)}",  # full-width digits
        f"{SHOPX}?cvc=" + quote("".join(chr(0x0660 + int(d)) for d in CVC)),  # Arabic-Indic
        f"{SHOPX}?cvc=+{CVC}+",  # plus signs as spaces around it
        f"{SHOPX}?q=" + quote(f"cvc={CVC}&a=1", safe=""),  # a query inside a value
        f"{SHOPX}?r=" + quote(f"/pay/{CVC}", safe=""),  # a path inside a value
        f"{SHOPX}?{CVC}",  # a bare token
        f"{SHOPX}?exp=12%2F{YY}",  # the expiry as typed
        f"{SHOPX}?exp=12/{YY}",
        f"{SHOPX}?exp=12-{YY}",
        f"{SHOPX}?exp=12+%2F+{YY}",
        f"{SHOPX}?exp=12{YEAR}",  # another format than the one typed
        f"https://shop.example.com/12%2F{YY}/x",
        f"{SHOPX}?name={NAME.upper()}",  # case-folded
        f"{SHOPX}?name={NAME[:4]}+{NAME[4:]}",  # a space, as a plus
        f"{SHOPX}?name={NAME[:4]}%20{NAME[4:]}",
        f"{SHOPX}?name={quote(_pct(NAME))}",
        f"{SHOPX}?zip={POSTAL.lower()}",
        f"{SHOPX}?zip={POSTAL[:3]}-{POSTAL[3:]}",
    ],
    ids=lambda _url: "u",  # never a generated value in a test id
)
def test_the_url_guard_blocks_every_typed_value_as_a_whole_parameter(url):
    _flow, g = _all_guard()
    assert g.carries(url) is True


@pytest.mark.parametrize(
    "url",
    [
        f"{SHOPX}?qty={CVC}9",  # the CVC inside a longer value: not equal
        f"{SHOPX}?qty=9{CVC}",
        f"{SHOPX}?order={CVC}{CVC}",
        f"https://shop.example.com/a{CVC}b/x",
        f"{SHOPX}?t=x{CVC}",
        f"{SHOPX}?exp=12{YY}9",
        f"{SHOPX}?n={NAME}x",
        f"{SHOPX}?n=x{NAME}",
        f"{SHOPX}?zip={POSTAL}0",
        f"{SHOPX}?last4=4242&amount=1999",
        "https://shop.example.com/",
    ],
    ids=lambda _url: "u",  # never a generated value in a test id
)
def test_the_url_guard_passes_a_value_only_inside_a_longer_one(url):
    _flow, g = _all_guard()
    assert g.carries(url) is False


def test_a_coincidental_equal_parameter_fails_closed():
    # The documented edge: equality to a whole parameter is enough, whatever
    # its name; ?qty=<the CVC> is blocked (a lost request, never a leak).
    _flow, g = _all_guard()
    assert g.carries(f"{SHOPX}?qty={CVC}") is True


def test_a_separate_month_and_year_are_blocked_only_together():
    _flow, g = _guard(exp_month="12", exp_year=str(YEAR))
    assert g.carries(f"{SHOPX}?m=12&y={YEAR}") is True
    assert g.carries(f"{SHOPX}?m=12&y={YY}") is True  # the year in two digits
    assert g.carries(f"{SHOPX}?page=12") is False  # a month alone is a common number
    assert g.carries(f"{SHOPX}?y={YEAR}") is False
    _flow, g = _guard(exp_month="03", exp_year=YY)
    assert g.carries(f"{SHOPX}?m=3&y={YEAR}") is True  # the option value without its zero


def test_a_value_too_short_to_match_safely_is_not_matched():
    _flow, g = _guard(name="Q z")  # normalises to two characters
    assert g.carries(f"{SHOPX}?n=qz") is False
    assert g.carries(f"{SHOPX}?card={PAN}") is True  # the number is still guarded


def test_the_guard_keeps_no_typed_value():
    _flow, g = _all_guard()
    held = repr(vars(g))
    for s in (PAN, CVC, NAME, POSTAL, NAME.casefold(), POSTAL.casefold(), f"12{YY}"):
        assert s not in held


def test_a_url_with_too_many_value_sized_tokens_is_unjudgeable():
    _flow, g = _all_guard()
    heavy = SHOPX + "?" + "&".join(f"v={i:03d}" for i in range(100, 1000)) * 5
    heavy += "&" + "&".join(f"w={i:04d}" for i in range(1000, 5200))
    with pytest.raises(pc._Unjudgeable):
        g.carries(heavy)


def test_a_redirect_hop_carrying_the_cvc_is_recorded_as_leaked():
    flow, g = _all_guard()
    g.on_request(_request(f"https://shop.example.com/land?cvc={CVC}"))
    assert flow.url_leaked is True


async def test_a_get_submit_during_the_click_without_the_number_is_blocked(vault, logs):
    # Round 7's P1s: a click handler unnames or rewrites the number field, then
    # the native default-GET submit sends the CVC and expiry alone. No DOM check
    # can see that; the network guard blocks the request whatever sent it.
    ns = checkout()
    hit: list = []

    def pay():
        r = Route(f"{SHOPX}?cvc={CVC}&exp=12%2F{YY}&name={NAME}")
        hit.append(r)
        asyncio.ensure_future(ns.pctx.route_handler(r))

    ns.els["pay"].on_click = pay
    out, _ = await _run(ns)
    assert hit[0].done == ("abort", "blockedbyclient")
    assert out["code"] == "card_url_blocked" and "do NOT retry" in out["status"]
    _sweep(out, logs)


async def test_the_tap_names_the_host_the_extras_moved_the_checkout_to(vault):
    # Filling an extra field moves the checkout to another host of the same site:
    # the tap names that host, and the page after the grant is the same page.
    ns = checkout()
    real_fill = Handle.fill

    async def fill(self, value, timeout=None):
        await real_fill(self, value, timeout)
        if self.el is ns.els["email"]:
            ns.ppage.same_document("https://pay.example.com/checkout")

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Handle, "fill", fill)
        out, _ = await _run(ns)
    assert vault.ops("release_request")[0]["host"] == "pay.example.com"
    assert out["code"] == "submitted_unverified" and out["host"] == "pay.example.com"


def test_a_month_option_matched_by_its_label_is_guarded_by_its_value():
    _flow, g = _guard(exp_month="12", exp_year=str(YEAR))
    url = f"{SHOPX}?m=mon12&y={YEAR}"
    assert g.carries(url) is False
    g.add_option("exp_month", "mon12")
    assert g.carries(url) is True


async def test_a_get_submit_of_a_label_matched_expiry_select_is_blocked(vault, logs):
    ns = checkout(select_expiry=True)
    month = ns.els["exp_month"]
    month.options = ["mon12", *[f"mon{m}" for m in range(1, 12)]]
    month.labels = {"mon12": "12"}  # only the label says 12: the value is the site's own
    hit: list = []

    def pay():
        r = Route(f"{SHOPX}?m=mon12&y={YEAR}")
        hit.append(r)
        asyncio.ensure_future(ns.pctx.route_handler(r))

    ns.els["pay"].on_click = pay
    out, _ = await _run(ns)
    assert hit[0].done == ("abort", "blockedbyclient")
    assert out["code"] == "card_url_blocked"
    _sweep(out, logs)


def test_a_name_sent_in_a_legacy_charset_is_blocked():
    name = NAME[:4] + "é" + NAME[4:]
    _flow, g = _guard(name=name)
    latin = "".join(f"%{b:02X}" for b in name.encode("latin-1"))
    assert g.carries(f"{SHOPX}?name={latin}") is True
    assert g.carries(f"{SHOPX}?name={quote(name)}") is True


def test_a_url_too_long_to_split_on_the_event_loop_is_unjudgeable():
    _flow, g = _all_guard()
    with pytest.raises(pc._Unjudgeable):
        g.carries(SHOPX + "?x=" + "a" * pc._MAX_URL)


async def test_a_short_value_match_while_typing_says_it_may_be_a_coincidence(vault, logs):
    # Only the CVC matched (?qty=<CVC> from a page script): Pay is not clicked,
    # and the result does not claim the card number was exposed.
    ns = checkout()
    clicked: list = []
    ns.els["pay"].on_click = lambda: clicked.append(1)
    real_fill = Handle.fill

    async def fill(self, value, timeout=None):
        await real_fill(self, value, timeout)
        if self.el is ns.els["cvc"]:
            await ns.pctx.route_handler(Route(f"https://t.example.org/c?qty={CVC}"))

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(Handle, "fill", fill)
        out, _ = await _run(ns)
    assert out["code"] == "card_value_in_request" and clicked == []
    assert "coincidence" in out["status"] and "not necessarily exposed" in out["status"]
    _sweep(out, logs)


def test_a_short_value_hop_keeps_the_number_flag_clear():
    flow, g = _all_guard()
    g.on_request(_request(f"https://shop.example.com/land?cvc={CVC}"))
    assert flow.url_leaked is True and flow.number_hit is False
    assert pc._typing_outcome(flow) == "card_value_in_request"
    g.on_request(_request(f"https://shop.example.com/land?pan={PAN}"))
    assert flow.number_hit is True and pc._typing_outcome(flow) == "card_url_before_pay"
