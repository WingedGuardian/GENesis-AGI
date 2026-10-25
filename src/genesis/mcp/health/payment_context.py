"""browser_fill_card's flow, in a browser context no other tool can reach.

The checkout the agent opened in the main Camoufox context is reopened in a
second, short-lived ``BrowserContext`` of the same browser: same device
fingerprint, the merchant's cookies and storage handed over, nothing else.
This module is the only one that creates, holds or closes that context, its
pages and its element handles (a contract test pins it). Every other browser
tool acts on ``browser._active_page``, which never points into it.

The flow, in order: check the checkout's card fields, Pay control and amount
in the payment context, fill the named non-card fields, ask the vault for a
release (the owner's Telegram tap shows the site, amount, card, Pay button
label, field origins and the extra field names), wait for the tap, re-check
the page, claim the card once, fill the card fields through element handles,
click Pay, classify what followed, and close the context. Every document of
the context is masked (fully transparent) from creation, so the display shows
nothing of the checkout. Every request of the context passes a network guard
(``_UrlGuard``) that, once the card is claimed, aborts any request whose URL
carries a typed card value (the first request of a redirect chain; a redirect
hop carrying one is seen and reported instead; a WebSocket URL is not covered).

The agent gets back only a fixed code and text, the merchant host, the kinds
of fields typed, and a vault reason from its fixed set: never page text, a
URL path, an exception's text or a value. Card values live in locals of one
coroutine and are dropped after the fill; the network guard keeps only a random
key, the number's length and keyed digests, for the context's life.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import hmac
import json
import logging
import re
import secrets
import unicodedata
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from urllib.parse import unquote_plus, urlsplit

from genesis.browser.profile import _is_public_suffix, domain_matches, registrable_domain
from genesis.payments import card_fill as cf
from genesis.payments import release_client as rc

logger = logging.getLogger(__name__)

STEP_S = 15.0  # one Playwright step (a count, a fill, a read-back)
# Before the tap only: how long a hosted widget may take to mount its frames,
# fields or Pay after DOMContentLoaded, or a total to render. After the tap an
# absent element means the page changed, so nothing waits there.
ATTACH_S = 10.0
EXPORT_S = 120.0  # the storage export: one temporary-page visit per origin seen since launch
GOTO_MS = 30000
POLL_S = 2.0
STATUS_FAILS = 5  # consecutive unanswered status polls before giving up
# The vault refuses a pending release 720 s after its request and a granted
# one 120 s after the tap; past both (plus slack) nothing can be granted.
CEILING_S = 900.0
CANCEL_S = 25.0
CLICK_MS = 10000
SETTLE_S = 60.0  # after the Pay click, for a navigation or a challenge
# After the URL changes, how long a challenge may still appear: a new document's
# load (up to 10 s) plus 3 s, and an in-page URL change gets the same wait.
NAV_GRACE_S = 13.0
TICK_S = 0.25
# How long the mask check keeps asking a frame that cannot answer (its context
# destroyed by a navigation, as a new iframe leaves about:blank). Such a frame
# was measured to wrongly refuse 16% of checkout loads when asked only once;
# it settles within a navigation, and it never counts as masked.
MASK_SETTLE_S = 5.0
CLOSE_S = 10.0
# The fallback close waits for the browser lock: a tool call holds it at most
# 300 s (_with_tool_timeout), then Camoufox's close steps take up to 10 s each.
CLOSE_BROWSER_S = 330.0
TEARDOWN_S = CANCEL_S + CLOSE_S + CLOSE_BROWSER_S + 5
ABORT_S = 70.0  # what an MCP exit or a reap waits; the teardown itself runs on
_TITLE_CHARS = 120  # the vault cuts to 80; this keeps the request line small
_NONCE = re.compile(r"[A-Za-z0-9_-]{1,64}")
# 3DS challenge pages, by host or path. Inferred, not measured: an unknown
# challenge still ends as "challenge" when the page does not move on in time.
_CHALLENGE_HOSTS = ("cardinalcommerce.com", "3dsecure.io", "hooks.stripe.com")
_CHALLENGE_PATH = re.compile(r"3d[-_]?secure|three[-_]?ds|/3ds|/acs\b", re.IGNORECASE)

# The mask (from the payment-tab guard design): the root element of every
# document is made fully transparent, so its whole subtree (shadow trees,
# iframes, canvas) draws nothing; controls get transparent text as a second
# layer. Opacity changes no layout or hit testing, so clicks still land.
MASK_CSS = (
    ":root { opacity: 0 !important }"
    " input, textarea, select, [contenteditable] {"
    " color: transparent !important; -webkit-text-fill-color: transparent !important;"
    " caret-color: transparent !important; text-shadow: none !important }"
    " ::selection { color: transparent !important; background: transparent !important }"
    # The top layer (a modal dialog, an open popover, a fullscreen element and
    # their backdrops) paints outside the root's opacity group, so the root rule
    # does not hide it. One rule per selector: an engine that does not know a
    # selector drops the whole rule it appears in, and the others must survive.
    " :modal { opacity: 0 !important }"
    " :popover-open { opacity: 0 !important }"
    " :fullscreen { opacity: 0 !important }"
    " ::backdrop { opacity: 0 !important }"
    # A view transition paints its pseudo-tree in an overlay too.
    " ::view-transition { opacity: 0 !important }"
    # The canvas takes the root's or body's background whatever the root's opacity.
    " html, body { background: transparent !important }"
)
# The top-layer states, as selectors; one at a time, since an engine that does
# not know one throws on it.
_TOP_LAYER = "[':modal', ':popover-open', ':fullscreen']"
_MASK_JS = (
    "() => { const id = '__genesis_payment_mask__';"
    " const root = document.documentElement;"
    " if (!root) return false;"
    " root.style.setProperty('opacity', '0', 'important');"
    " let s = document.getElementById(id);"
    " if (!s) { s = document.createElement('style'); s.id = id;"
    " (document.head || root).appendChild(s); }"
    " s.removeAttribute('media'); s.disabled = false;"
    " if (s.textContent !== '" + MASK_CSS + "') s.textContent = '" + MASK_CSS + "';"
    # A page rule or inline style marked important beats the stylesheet; an
    # inline important set after it wins, so each open top-layer element gets one.
    " for (const q of " + _TOP_LAYER + ") { try { for (const e of document.querySelectorAll(q))"
    " e.style.setProperty('opacity', '0', 'important'); } catch (_) {} }"
    " return true; }"
)
# A context-level init script: runs before the page's own scripts in every
# document and frame of the payment context, popups included. Its observer stays
# for the document's life: a page script that removes the style or the inline
# opacity (say in an input event during a fill) is undone in the same microtask
# checkpoint, before the next paint. It re-applies only what is missing, so its
# own writes do not re-trigger it.
MASK_INIT_JS = (
    "(() => { const m = " + _MASK_JS + ";"
    " const ok = () => { const r = document.documentElement;"
    " const s = document.getElementById('__genesis_payment_mask__');"
    " return !!r && r.style.getPropertyValue('opacity') === '0'"
    " && r.style.getPropertyPriority('opacity') === 'important'"
    " && !!s && !s.disabled && !s.hasAttribute('media') && s.textContent === '" + MASK_CSS + "'"
    " && " + _TOP_LAYER + ".every((q) => { try { return Array.from(document.querySelectorAll(q))"
    ".every((e) => e.style.getPropertyValue('opacity') === '0'"
    " && e.style.getPropertyPriority('opacity') === 'important'); } catch (_) { return true; } }); };"
    " new MutationObserver(() => { if (!ok()) m(); }).observe(document,"
    " {childList: true, subtree: true, attributes: true, characterData: true});"
    # Opening a popover or going fullscreen changes no attribute: catch the
    # events instead (beforetoggle runs before the popover first paints).
    " const hide = (ev) => { const t = ev.target;"
    " if (t && t.style) t.style.setProperty('opacity', '0', 'important'); m(); };"
    " for (const ev of ['beforetoggle', 'toggle', 'fullscreenchange'])"
    " document.addEventListener(ev, hide, true);"
    " m(); document.addEventListener('DOMContentLoaded', m); })();"
)
_MASK_CHECK_JS = """() => {
  const clear = (v) => v === 'transparent' || /^rgba\\([^)]*,\\s*0\\)$/.test(v);
  const root = document.documentElement;
  if (!root || getComputedStyle(root).opacity !== '0') return false;
  for (const e of [root, document.body]) {
    if (!e) continue;
    const s = getComputedStyle(e);
    if (!clear(s.backgroundColor) || s.backgroundImage !== 'none') return false;
  }
  try {  // an engine without view transitions has nothing there to paint
    if (getComputedStyle(root, '::view-transition').opacity !== '0') return false;
  } catch (_) {}
  for (const e of document.querySelectorAll('input, textarea, select')) {
    const s = getComputedStyle(e);
    if (!clear(s.color) || !clear(s.webkitTextFillColor) || !clear(s.caretColor)) return false;
    if (s.textShadow && s.textShadow !== 'none') return false;
  }
  for (const sel of [':modal', ':popover-open', ':fullscreen']) {
    let top;
    try { top = document.querySelectorAll(sel); } catch (_) { continue; }  // unknown to this engine
    for (const e of top) {
      if (getComputedStyle(e).opacity !== '0') return false;
      if (getComputedStyle(e, '::backdrop').opacity !== '0') return false;
    }
  }
  return true;
}"""
# Whether this document lacks the mask altogether (the init script never ran in
# it), as opposed to a mask that is present but defeated.
_MASK_ABSENT_JS = "() => !document.getElementById('__genesis_payment_mask__')"
# Whether ``e`` is a field of ``kind``: an enabled input or select (``:disabled``,
# so a disabled ancestor fieldset counts, as Playwright's fill sees it) whose
# autocomplete names that kind, or a type=tel input (a select for an expiry
# month or year) whose label, aria-label, placeholder, name or id says so.
# Returns the tag, or '' when it is not.
CARD_FIELD_JS = r"""(e, kind) => {
  const tag = e.tagName.toLowerCase();
  if (!['input', 'select'].includes(tag) || e.matches(':disabled') || e.readOnly) return '';
  const type = (e.getAttribute('type') || 'text').toLowerCase();
  const bad = ['hidden', 'file', 'checkbox', 'radio', 'submit', 'button', 'image',
               'reset', 'range', 'color'];
  if (tag === 'input' && bad.includes(type)) return '';
  if (type === 'password' && kind !== 'cvc') return '';  // some checkouts mask the CVC
  const tokens = {number: ['cc-number'], expiry: ['cc-exp'], exp_month: ['cc-exp-month'],
                  exp_year: ['cc-exp-year'], cvc: ['cc-csc'], name: ['cc-name'],
                  postal_code: ['postal-code']}[kind];
  if (!tokens) return '';
  const ac = (e.getAttribute('autocomplete') || '').toLowerCase().split(/\s+/);
  if (tokens.some((t) => ac.includes(t))) return tag;
  const label = {number: /card\s*(number|no\b|#)|card\s*num/i,
                 expiry: /expir|exp\.?\s*date|mm\s*\/\s*yy/i,
                 exp_month: /month|\bmm\b/i,
                 exp_year: /year|\byy(yy)?\b/i,
                 cvc: /\b(cvc|cvv|csc|cid)\b|security\s*code|card\s*code/i}[kind];
  if (!label) return '';
  const shaped = tag === 'select' ? ['exp_month', 'exp_year'].includes(kind) : type === 'tel';
  if (!shaped) return '';
  const text = [e.getAttribute('aria-label'), e.getAttribute('placeholder'),
                e.getAttribute('name'), e.id,
                ...Array.from(e.labels || []).map((l) => l.textContent)]
    .filter(Boolean).join(' ');
  return label.test(text) ? tag : '';
}"""
# The Pay control: a button (not type=reset) or an input type=submit; whether
# its form DECLARES GET (a formmethod or method attribute whose value, or invalid
# value, means GET); whether it can be clicked; its label. A form that is GET
# only because it has no method attribute is how script-handled checkouts look
# (the script cancels the submit and pays by its own request), so it is not
# refused here: the payment context's network guard (_UrlGuard) blocks any
# request whose URL carries a typed card value (see its docstring for the limits).
PAY_JS = r"""(e) => {
  const tag = e.tagName.toLowerCase();
  const type = (e.getAttribute('type') || '').toLowerCase();
  const button = tag === 'button' ? type !== 'reset' : tag === 'input' && type === 'submit';
  const f = e.form;
  // Through the prototypes: a form control named "method" or "hasAttribute"
  // shadows the form's own property of that name (legacy named access).
  const has = (x, a) => Element.prototype.hasAttribute.call(x, a);
  const formMethod = Object.getOwnPropertyDescriptor(HTMLFormElement.prototype, 'method').get;
  const m = !f ? '' : has(e, 'formmethod') ? e.formMethod
    : has(f, 'method') ? formMethod.call(f) : '';
  const label = tag === 'input' ? (e.value || '') : (e.innerText || e.textContent || '');
  return {button, get: String(m).toLowerCase() === 'get',
          live: e.isConnected && !e.matches(':disabled'), label: label.slice(0, 200)};
}"""
# An extra (non-card) field: an input, textarea or select whose autocomplete
# names no card token. Returns the tag, or ''.
EXTRA_FIELD_JS = r"""(e) => {
  const tag = e.tagName.toLowerCase();
  if (!['input', 'textarea', 'select'].includes(tag) || e.matches(':disabled') || e.readOnly)
    return '';
  const ac = (e.getAttribute('autocomplete') || '').toLowerCase();
  return /(^|\s)cc-/.test(ac) ? 'card' : tag;
}"""
# What an extra field now holds: its value, and for a select the chosen labels.
EXTRA_VALUE_JS = (
    "(e) => e.tagName.toLowerCase() === 'select'"
    " ? [e.value, ...Array.from(e.selectedOptions).map((o) => o.label)] : [e.value]"
)
# The page's sessionStorage, which storage_state() does not carry.
_SESSION_JS = (
    "() => { const o = {}; for (let i = 0; i < sessionStorage.length; i++) {"
    " const k = sessionStorage.key(i); o[k] = sessionStorage.getItem(k); } return o; }"
)
_SAME_JS = "(a, b) => a === b"
_TEXT_JS = "() => (document.body ? document.body.innerText : '')"


class PaymentInFlight(Exception):
    """A browser restart refused because a claimed card is in the payment context."""


@dataclass(frozen=True)
class Hooks:
    """What the flow needs from browser.py, which owns the main browser."""

    alive: Callable[[], bool]  # the main Camoufox context is still the one the flow began on
    touch: Callable[[], None]  # keep Camoufox's idle clock from reclaiming it
    interstitial: Callable[[object], Awaitable[bool]]  # a bot-check page?
    close_browser: Callable[[], Awaitable[None]]  # close all of Camoufox (takes its lock)


@dataclass(eq=False)
class _Field:
    frame: object
    handle: object
    tag: str
    origin: str


@dataclass(eq=False)
class _Flow:
    task: asyncio.Task | None
    host: str = ""
    pctx: object = None
    nonce: str | None = None
    claimed: bool = False
    typing: bool = False
    typed: list[str] = field(default_factory=list)
    paid: bool = False
    popup: bool = False
    popups: int = 0
    navs: int = 0  # top-document commits, counted from the Pay click
    nav_requests: int = 0  # top-document navigation requests, sent or not yet committed
    typing_marks: tuple[int, int] | None = None  # (nav_requests, popups) as typing began
    url_blocked: bool = False  # the network guard aborted a request carrying a card value
    url_leaked: bool = False  # a redirect hop carried a card value (a route cannot stop it)
    number_hit: bool = False  # one of those carried the number (else only a short value)
    abort_reason: str | None = None
    tearing_down: bool = False
    stuck: bool = False  # the fallback browser close was not reached
    hooks: Hooks | None = None
    report: Callable[[str], Awaitable[None]] | None = None
    granted: bool = False  # past the tap: keep the client's idle clock fed
    done: asyncio.Event = field(default_factory=asyncio.Event)


# What may sit between a value's characters in a URL and still read as it:
# spaces, plus signs, dots, slashes, dashes and underscores (value_landed in
# card_fill accepts a slash as card formatting too). ``&`` and ``=`` still end a
# run, so a match needs the whole number inside one parameter or path segment.
_URL_SEPARATORS = re.compile(r"[\s.+/_-]+")
# Where a URL's parameters and path segments end, for the whole-value match.
_PARAM_ENDS = re.compile(r"[?&;#]")
# The guard computes one keyed digest per digit window or candidate token, on the
# event loop that also runs the flow: about 6 us each (measured, so 1M digits took
# 6.4 s). A URL needing more than this (about 25 ms of work) is not judged; the
# route aborts it, unrecorded. No real checkout URL comes near: it takes 4,096
# digit windows, a single run over 4,000 digits long for a 16-digit card, or
# 4,096 distinct tokens of a typed value's length.
_MAX_WINDOWS = 4096
_MIN_VALUE = 3  # a shorter normalised name or postal code is not matched (noise)
# A URL longer than this is not judged (aborted, unrecorded): splitting and
# normalising it runs on the event loop, about 1.3 s per MB (measured).
_MAX_URL = 65536


def _norm(text: str) -> str:
    """A value as the guard compares it: NFKC, case-folded, any Unicode decimal
    digit as its ASCII digit, separators removed."""
    text = unicodedata.normalize("NFKC", text).casefold()
    if not text.isascii():
        text = "".join(
            str(unicodedata.decimal(c)) if not c.isascii() and c.isdecimal() else c for c in text
        )
    return _URL_SEPARATORS.sub("", text)


def _decoded(text: str) -> str:
    return unquote_plus(unquote_plus(text))


def _tokens(url: str) -> set[str]:
    """Every whole parameter key, value and path segment of ``url``, normalised:
    split raw and after each of two decodings, as UTF-8 and as Latin-1 (a form
    with a legacy charset), so a value encoded inside another becomes its own
    parameter; each piece decoded twice."""
    out = set()
    latin = unquote_plus(url, encoding="latin-1")
    for text in {url, unquote_plus(url), _decoded(url), latin, unquote_plus(latin, "latin-1")}:
        for piece in _PARAM_ENDS.split(text):
            parts = {piece, *piece.split("="), *piece.split("/")}
            if "=" in piece:
                value = piece.split("=", 1)[1]
                parts |= {value, *value.split("/")}
            out.update(_norm(_decoded(p)) for p in parts)
    out.discard("")
    return out


class _Unjudgeable(Exception):
    """A URL too heavy to check within _MAX_WINDOWS."""


class _UrlGuard:
    """The network half of "card values never leave in a URL".

    Routed on the whole payment context before its first page loads, so the
    first request of every redirect chain, in every frame and popup, passes
    through ``handle``: a native GET form, a page's ``form.submit()``, a link,
    ``location``, ``fetch``, a beacon. Armed after the claim with every value
    the fill types; from then on a request whose URL carries one is aborted and
    recorded on the flow (``url_blocked``):

    - the number: anywhere in a digit run (decoded twice; spaces, plus signs,
      dots, slashes, dashes and underscores ignored);
    - the CVC, the expiry (MMYY and MMYYYY, whatever the format typed), the
      name and the postal code: a whole parameter key or value, or a whole path
      segment, EQUAL to the value after ``_norm`` (``_tokens``). Equality, not a
      substring: a 3-digit CVC is a substring of most URLs (measured), but rarely
      a whole parameter. A coincidental equal parameter (``?qty=123`` when the
      CVC is 123) is still blocked: that fails closed, and costs a request;
    - a separate expiry month and year: only both in one URL (each alone is a
      common small number).

    A request the guard cannot judge is aborted too, without the record (it does
    not know the card was in it). This guards a trusted merchant's flawed form:
    a page script that encodes a value its own way (base64, split, encrypted)
    is not caught.

    What a route cannot stop: a redirect hop (Playwright calls the handler for
    the first URL of a chain only) and a WebSocket URL. A redirect hop that
    carries a value is still SEEN, by ``on_request``, and recorded as
    ``url_leaked``: it already left, so the flow reports it rather than claiming
    it was blocked. A WebSocket URL is neither stopped nor seen.

    The values themselves are not kept: only a random key, the number's length
    and keyed digests, which outlive the fill for the context's life (a
    low-entropy value such as the CVC is recoverable from them by enumeration,
    by whatever can read this process's memory while the context lives).
    """

    def __init__(self, flow: _Flow) -> None:
        self._flow = flow
        self.main_frame = None  # the payment page's, once it exists
        self._key = b""
        self._len = 0
        self._tag: bytes | None = None
        self._whole: set[bytes] = set()  # digests of values matched whole
        self._pair: tuple[set[bytes], set[bytes]] = (set(), set())  # month forms, year forms
        self._lens: set[int] = set()  # the normalised lengths of all of them

    def _mac(self, text: str) -> bytes:
        return hmac.new(self._key, text.encode(), "sha256").digest()

    def _digests(self, forms) -> set[bytes]:
        normed = {n for n in (_norm(f) for f in forms) if n}
        self._lens |= {len(n) for n in normed}
        return {self._mac(n) for n in normed}

    def arm(self, values: dict[str, str]) -> None:
        """``values``: kind -> the text typed (card_fill.values_for)."""
        self._key = secrets.token_bytes(32)
        digits = _norm(re.sub(r"\D", "", values["number"]))
        self._len = len(digits)
        self._tag = self._mac(digits)
        del digits
        for kind, value in values.items():
            if kind == "expiry":
                d = re.sub(r"\D", "", _norm(value))  # MMYY or MMYYYY, whatever was typed
                if len(d) == 6:
                    d = d[:2] + d[4:]
                self._whole |= self._digests([d, d[:2] + "20" + d[2:]])
            elif kind in ("cvc", "name", "postal_code"):
                if len(_norm(value)) >= _MIN_VALUE:
                    self._whole |= self._digests([value])
        if "exp_month" in values and "exp_year" in values:
            mm, yy = int(values["exp_month"]), int(values["exp_year"]) % 100
            self._pair = (
                self._digests([f"{mm:02d}", str(mm)]),
                self._digests([f"{yy:02d}", f"{2000 + yy}"]),
            )

    def add_option(self, kind: str, value: str) -> None:
        """A separate expiry select took an option whose value is not one of the
        forms armed (matched by its label): that value is what a form sends."""
        months, years = self._pair
        if kind in ("exp_month", "exp_year") and months and years:
            (months if kind == "exp_month" else years).update(self._digests([value]))

    def carries(self, url: str) -> bool:
        """Whether ``url`` carries an armed value. Raises _Unjudgeable when the
        check would take more than _MAX_WINDOWS digests."""
        return self._judge(url) is not None

    def _judge(self, url: str) -> str | None:
        """ "number", "value" (only a short value matched: maybe a coincidence) or None."""
        if self._tag is None:
            return None
        if len(url) > _MAX_URL:
            raise _Unjudgeable
        text = _norm(_decoded(url))
        n = self._len
        runs = re.findall(rf"\d{{{n},}}", text) if n else []
        windows = sum(len(run) - n + 1 for run in runs)
        cands = [t for t in _tokens(url) if len(t) in self._lens] if self._lens else []
        if windows + len(cands) > _MAX_WINDOWS:
            raise _Unjudgeable
        for run in runs:
            for i in range(len(run) - n + 1):
                if hmac.compare_digest(self._mac(run[i : i + n]), self._tag):
                    return "number"
        macs = {self._mac(t) for t in cands}
        months, years = self._pair
        return "value" if macs & self._whole or (macs & months and macs & years) else None

    async def handle(self, route) -> None:
        try:
            hit = self._judge(route.request.url)
        except Exception as e:  # noqa: BLE001 - never the URL
            logger.warning("payment url guard could not judge a request (%s)", type(e).__name__)
            with contextlib.suppress(Exception):
                await route.abort("failed")
            return
        if hit:
            self._flow.url_blocked = True
            self._flow.number_hit |= hit == "number"
            with contextlib.suppress(Exception):
                await route.abort("blockedbyclient")
            return
        try:
            await route.continue_()
        except Exception as e:  # noqa: BLE001 - the request stays paused: it never leaves
            logger.warning("payment url guard could not pass a request on (%s)", type(e).__name__)

    def on_request(self, request) -> None:
        """The context's request event: a top-level navigation request (counted:
        a checkout can submit itself while the card is typed, and a request
        exists before its document commits; a fragment or history change sends
        none), and a redirect hop that carries the number."""
        if self.main_frame is not None:
            try:
                if request.is_navigation_request() and request.frame is self.main_frame:
                    self._flow.nav_requests += 1
            except Exception:  # noqa: BLE001 - a request with no frame (a worker's)
                pass
        if getattr(request, "redirected_from", None) is None:
            return
        try:
            leaked = self._judge(request.url)
        except Exception as e:  # noqa: BLE001 - never the URL
            # Too heavy to check: counted as a leak, the outcome cannot be vouched for.
            logger.warning("payment url guard could not judge a redirect (%s)", type(e).__name__)
            leaked = "number" if self._tag is not None else None
        if leaked:
            self._flow.url_leaked = True
            self._flow.number_hit |= leaked == "number"


# _outcome's "the checkout did not move on within the bound" (mapped in _run).
_NO_SETTLE = "no_settle"


class _Stop(Exception):
    def __init__(self, code: str, **extra) -> None:
        super().__init__(code)
        self.code, self.extra = code, extra


_flow: _Flow | None = None
_x_input = 0  # Turnstile X-input phases running in this process
_prefs_pinned = False
_tasks: set[asyncio.Task] = set()


def set_prefs_pinned(pinned: bool) -> None:
    """Called by each Camoufox launch: whether it pinned ``CARD_FILL_PREFS``."""
    global _prefs_pinned
    _prefs_pinned = pinned


def prefs_pinned() -> bool:
    return _prefs_pinned


def _held() -> _Flow | None:
    """The running flow; a stuck one (its fallback close not reached) only while
    the browser it ran on is still the live one, so the guard retires with it."""
    flow = _flow
    if flow is not None and flow.stuck and not (flow.hooks and flow.hooks.alive()):
        return None
    return flow


def in_flight() -> bool:
    """A fill holds the payment context or is still tearing it down."""
    return _held() is not None


def past_claim() -> bool:
    """A card was claimed and the flow has not finished tearing down."""
    flow = _held()
    return flow is not None and flow.claimed


def is_payment_page(page) -> bool:
    """Whether ``page`` belongs to the payment context (no tool may act on it)."""
    flow = _flow
    return (
        flow is not None and flow.pctx is not None and getattr(page, "context", None) is flow.pctx
    )


@contextlib.contextmanager
def try_x_input():
    """Turnstile's side of the one lock: yields False while a payment runs,
    else True, and a fill started meanwhile refuses ``turnstile_in_progress``."""
    global _x_input
    if in_flight():
        yield False
        return
    _x_input += 1
    try:
        yield True
    finally:
        _x_input -= 1


def _spawn(coro) -> asyncio.Task:
    """A task held until it ends, whose exception is retrieved here (asyncio's
    own logger has no card filter)."""
    task = asyncio.ensure_future(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    return task


async def abort(reason: str) -> None:
    """End a running fill now (MCP exit, the main browser closing) and wait,
    bounded, for its teardown: a release not yet claimed is cancelled."""
    flow = _flow
    if flow is None:
        return
    flow.abort_reason = reason
    task = flow.task
    if task and not task.done() and not flow.tearing_down and task is not asyncio.current_task():
        task.cancel()
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(flow.done.wait(), timeout=ABORT_S)


async def fill_card(args: cf.CardArgs, main_ctx, main_page, hooks: Hooks, report) -> dict:
    """The whole flow; returns a fixed result. ``main_ctx`` and ``main_page``
    are the Camoufox context and active page, read once by the caller."""
    global _flow
    if in_flight():
        return cf.result("payment_in_flight")
    if _x_input:
        return cf.result("turnstile_in_progress")
    flow = _flow = _Flow(task=asyncio.current_task(), hooks=hooks, report=report)
    try:
        try:
            return await _run(flow, args, main_ctx, main_page, hooks, report)
        except _Stop as s:
            return cf.result(s.code, flow.host, **s.extra)
        except asyncio.CancelledError:
            if flow.abort_reason is None:
                if flow.claimed:  # the card is spent, and Pay may have landed: say so
                    logger.error(
                        "payment cancelled by its caller after the claim (paid=%s, typed=%d)",
                        flow.paid,
                        len(flow.typed),
                    )
                raise
            asyncio.current_task().uncancel()
            return _ended(flow, "browser_closed")
        except Exception as e:  # noqa: BLE001 - never an exception's text: it may quote a value
            logger.error("payment fill step failed (%s)", type(e).__name__)
            return _ended(flow, "internal_error")
    finally:
        # Shielded and held: a cancelled caller cannot skip the cancel or the close.
        flow.tearing_down = True  # an abort from here waits for it, keeping this result
        await asyncio.shield(_spawn(asyncio.wait_for(_teardown(flow, hooks), TEARDOWN_S)))


def _ended(flow: _Flow, before_claim: str) -> dict:
    """The result for a flow cut short, by how far it got."""
    if flow.paid:
        return cf.result("outcome_unknown", flow.host)
    if flow.typing:
        return cf.result(_typing_outcome(flow) or "failed_before_pay", flow.host, filled=flow.typed)
    return cf.result("claimed_not_filled" if flow.claimed else before_claim, flow.host)


async def _step(aw):
    out = await asyncio.wait_for(aw, timeout=STEP_S)
    await _beat()
    return out


async def _beat() -> None:
    """Progress after the grant (the reporter sends at most one per 5 s, each
    bounded): the claim, the fill and the outcome can outlast an MCP client's
    idle timeout, and a cancel there can spend a single-use card unpaid."""
    flow = _flow
    if flow is not None and flow.granted and flow.report is not None:
        with contextlib.suppress(Exception):
            await flow.report("finishing the approved payment")


async def _attached(loc, wait: bool) -> None:
    """Before the tap, give a late-mounting element time to appear."""
    if wait:
        with contextlib.suppress(Exception):
            await _step(loc.first.wait_for(state="attached", timeout=ATTACH_S * 1000))


async def _teardown(flow: _Flow, hooks: Hooks) -> None:
    global _flow
    escalating = False
    try:
        if flow.nonce is not None and not flow.claimed:
            try:
                await asyncio.wait_for(rc.cancel(flow.nonce), timeout=CANCEL_S)
            except Exception as e:  # noqa: BLE001 - the vault expires it on its own
                logger.warning("payment release cancel failed (%s)", type(e).__name__)
        if flow.pctx is not None:
            try:
                await asyncio.wait_for(flow.pctx.close(), timeout=CLOSE_S)
            except Exception as e:  # noqa: BLE001
                logger.error("payment context did not close (%s)", type(e).__name__)
                if hooks.alive():  # it may hold typed values: close the whole browser
                    escalating = True
                    await asyncio.wait_for(hooks.close_browser(), CLOSE_BROWSER_S)
    except Exception as e:  # noqa: BLE001
        logger.error("payment teardown step failed (%s)", type(e).__name__)
    finally:
        # A close that did not detach the browser (refused, timed out, cancelled):
        # the guard stays while that browser lives, and the close is tried again.
        if escalating and hooks.alive():
            logger.error("payment browser close not reached; payments stay refused")
            flow.stuck = True
            _spawn(hooks.close_browser())
        if _flow is flow and not flow.stuck:
            _flow = None
        flow.done.set()


async def _owned_by(handle, frame) -> bool:
    """The element lives in ``frame`` itself. A locator can step into a child
    frame (selector grammar ``card_fill`` refuses, checked here again on the
    element), and an origin read off ``frame`` would then not be the element's."""
    return await _step(handle.owner_frame()) is frame


async def _walk(frame, frames: tuple[str, ...], through: list | None = None, wait=False):
    """The frame a chain of iframe selectors leads to, or None; ``through``
    collects every frame entered on the way, the last one included."""
    for fsel in frames:
        loc = frame.locator(fsel)
        await _attached(loc, wait)
        if await _step(loc.count()) != 1:
            return None
        handle = await _step(loc.element_handle(timeout=5000))
        try:
            if not await _owned_by(handle, frame):
                return None
            frame = await _step(handle.content_frame())
        finally:
            with contextlib.suppress(Exception):
                await _step(handle.dispose())
        if frame is None:
            return None
        if through is not None:
            through.append(frame)
    return frame


async def _resolve_one(page, spec: cf.Spec, kind: str, wait=False) -> tuple[object, object, str]:
    frames, sel = spec
    frame = await _walk(page.main_frame, frames, wait=wait)
    if frame is None:
        raise _Stop(f"frame_not_found:{kind}")
    loc = frame.locator(sel)
    await _attached(loc, wait)
    count = await _step(loc.count())
    if count != 1:
        raise _Stop(
            "pay_not_found"
            if kind == "pay"
            else f"field_{'not_found' if count == 0 else 'ambiguous'}:{kind}"
        )
    origin = cf.origin_of(frame.url)
    if not cf.secure_origin(origin):  # about:blank, srcdoc, data: or plain http: never
        raise _Stop(f"field_origin:{kind}")
    handle = await _step(loc.element_handle(timeout=5000))
    if not await _owned_by(handle, frame):
        raise _Stop(f"field_origin:{kind}")
    return frame, handle, origin


async def _resolve(page, args: cf.CardArgs, wait=False) -> tuple[dict[str, _Field], _Field, str]:
    """Each card field and the Pay control, resolved to exactly one element and
    checked for meaning on it; plus Pay's label. The fill and the click go
    through these handles, so a swapped element or a navigated frame makes them
    fail instead of acting on a document the tap did not show."""
    fields = {}
    for kind, spec in args.fields.items():
        frame, handle, origin = await _resolve_one(page, spec, kind, wait)
        tag = await _step(handle.evaluate(CARD_FIELD_JS, kind))
        if tag not in ("input", "select"):
            raise _Stop(f"field_mismatch:{kind}")
        for other in fields.values():  # two kinds on one element: refused before the tap
            if other.frame is frame and await _step(handle.evaluate(_SAME_JS, other.handle)):
                raise _Stop(f"field_ambiguous:{kind}")
        fields[kind] = _Field(frame, handle, tag, origin)
    frame, handle, origin = await _resolve_one(page, args.pay, "pay", wait)
    info = await _step(handle.evaluate(PAY_JS))
    if not isinstance(info, dict) or info.get("button") is not True:
        raise _Stop("pay_not_button")
    if info.get("get") is not False:  # it would put the card in a URL: refused before the tap
        raise _Stop("pay_get_form")
    label = info.get("label")
    return fields, _Field(frame, handle, "pay", origin), label if isinstance(label, str) else ""


async def _mask_ok(page) -> bool:
    """Every frame is masked. A frame below the top document with NO mask at
    all gets it and is re-checked: Camoufox's init script misses some nested
    frames (measured: four 1-px hidden helper frames of a hosted card widget).
    An in-flow frame draws nothing while the top document is masked, since
    opacity applies to a whole subtree, iframes included; a frame in the top
    layer does not, which is why the mask covers the top layer itself. A mask
    that is present but defeated is never repaired, and neither is the top
    document: either fails the check.

    A frame that cannot answer is asked again, the whole pass from the start,
    for up to MASK_SETTLE_S; a frame that answers "not masked" fails at once."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + MASK_SETTLE_S
    while True:
        try:
            frames = list(page.frames)
            for frame in frames:
                if await _step(frame.evaluate(_MASK_CHECK_JS)) is True:
                    continue
                if (
                    frame.parent_frame is None
                    or await _step(frame.evaluate(_MASK_ABSENT_JS)) is not True
                ):
                    return False
                await _step(frame.evaluate(MASK_INIT_JS))
                if await _step(frame.evaluate(_MASK_CHECK_JS)) is not True:
                    return False
            return bool(frames)
        except Exception as e:  # noqa: BLE001
            if loop.time() >= deadline:
                logger.warning("payment mask check failed (%s)", type(e).__name__)
                return False
        await asyncio.sleep(TICK_S)


async def _amount_ok(page, amount: str) -> bool:
    """The stated amount is in the page text. The text is read here and dropped:
    it is never returned or logged."""
    parts = []
    for frame in list(page.frames):
        with contextlib.suppress(Exception):
            parts.append(await _step(frame.evaluate(_TEXT_JS)))
    ok = cf.amount_on_page(amount, "\n".join(p for p in parts if isinstance(p, str)))
    parts.clear()
    return ok


def _top(flow: _Flow, ppage, site: str) -> tuple[str, str]:
    """The payment page's host and origin, on the checkout's site over https."""
    host, top = urlsplit(ppage.url).hostname, cf.origin_of(ppage.url)
    if not host or top is None or registrable_domain(host) != site:
        raise _Stop("checkout_redirected")
    if not cf.secure_origin(top):
        raise _Stop("not_https")
    flow.host = host
    return host, top


def _gone(flow: _Flow, ppage, hooks: Hooks) -> str | None:
    if not hooks.alive():
        return "browser_closed"
    if ppage.is_closed():
        return "checkout_closed"
    return None


def _moved(ppage, top: str, fields: dict[str, _Field], pay: _Field, seen: dict) -> bool:
    """The page no longer shows what the tap was for: another top origin, or a
    field's or Pay's frame not on the origin seen before the tap."""
    try:
        if cf.origin_of(ppage.url) != top:
            return True
        return any(
            cf.origin_of(f.frame.url) != seen.get(k) for k, f in [*fields.items(), ("pay", pay)]
        )
    except Exception:  # noqa: BLE001
        return True


async def _extras(ppage, args: cf.CardArgs, fields: dict[str, _Field], fill: bool) -> None:
    """Each extra field: exactly one input, textarea or select in the top
    document, no card field, no two names on one element; filled when ``fill``,
    and then holding its value (checked again after the tap and before Pay)."""
    seen: list = []
    for sel, value in args.extra.values():
        loc = ppage.main_frame.locator(sel)
        await _attached(loc, fill)
        if await _step(loc.count()) != 1:
            raise _Stop("extra_field_not_found")
        handle = await _step(loc.element_handle(timeout=5000))
        if not await _owned_by(handle, ppage.main_frame):
            raise _Stop("extra_field_not_found")
        tag = await _step(handle.evaluate(EXTRA_FIELD_JS))
        if tag == "card":
            raise _Stop("extra_field_is_card_field")
        if tag not in ("input", "textarea", "select"):
            raise _Stop("extra_field_not_found")
        for f in fields.values():
            if f.frame is ppage.main_frame and await _step(handle.evaluate(_SAME_JS, f.handle)):
                raise _Stop("extra_field_is_card_field")
        for other in seen:
            if await _step(handle.evaluate(_SAME_JS, other)):
                raise _Stop("extra_field_not_found")
        seen.append(handle)
        if fill:
            try:
                if tag == "select":
                    await _select(handle, value)
                else:
                    await _step(handle.fill(value, timeout=10000))
            except Exception:  # noqa: BLE001
                raise _Stop("extra_field_failed") from None
    for handle, (_sel, value) in zip(seen, args.extra.values(), strict=True):
        got = await _step(handle.evaluate(EXTRA_VALUE_JS))
        if not isinstance(got, list) or value.strip().casefold() not in {
            g.strip().casefold() for g in got if isinstance(g, str)
        }:
            raise _Stop("extra_field_failed")


async def _title(ppage) -> str:
    """The page title as the tap shows it (capped)."""
    title = await _step(ppage.title())
    return (title if isinstance(title, str) else "")[:_TITLE_CHARS]


async def _same_page(
    ppage, args, top, seen, label, title, fields, pay, hooks, clicking=False
) -> bool:
    """Everything the tap showed still holds: the top and frame origins, the
    page title, Pay (the same label, a button, no GET submit), the mask, the
    amount, and every extra field's value. Run after the tap, after the claim,
    and before Pay."""
    if _moved(ppage, top, fields, pay, seen):
        return False
    if await _title(ppage) != title:  # a same-origin SPA can swap the order under one URL
        return False
    if await hooks.interstitial(ppage):  # a bot check over the form takes the input
        return False
    if not _pay_ok(await _step(pay.handle.evaluate(PAY_JS)), label, clicking):
        return False
    if not await _mask_ok(ppage) or not await _amount_ok(ppage, args.amount):
        return False
    try:
        await _extras(ppage, args, fields, fill=False)
    except _Stop:
        return False
    return True


def _pay_ok(info, label: str, clicking: bool) -> bool:
    """Pay is still the approved control: a button, not a GET submit, the label
    the tap showed; and, to click it, connected and enabled."""
    return (
        isinstance(info, dict)
        and info.get("button") is True
        and info.get("get") is False
        and info.get("label") == label
        and (not clicking or info.get("live") is True)
    )


async def _holds(f: _Field, kind: str, value: str) -> bool:
    """The field shows the value typed into it (read back here and dropped). A
    select may have been matched by an option's label (``_select``), so its
    value or its selected label may show it."""
    if f.tag == "select":
        got = await _step(f.handle.evaluate(EXTRA_VALUE_JS))
        return isinstance(got, list) and any(cf.value_landed(kind, value, g) for g in got)
    return cf.value_landed(kind, value, await _step(f.handle.input_value(timeout=5000)))


async def _select(handle, value: str, kind: str = "") -> None:
    """A select: by option value, then label; an expiry month or year also in
    the forms a site uses (01 or 1; 2034 or 34)."""
    cands = [value]
    if kind == "exp_month":
        cands.append(str(int(value)))
    elif kind == "exp_year":
        cands += [value[-2:], str(2000 + int(value) % 100)]
    for how in ("value", "label"):
        for cand in dict.fromkeys(cands):
            with contextlib.suppress(Exception):
                await _step(handle.select_option(**{how: cand}, timeout=2000))
                return
    raise RuntimeError("no matching option")


async def _await_grant(flow: _Flow, ppage, hooks: Hooks, report) -> None:
    """Poll release_status until granted; raises _Stop otherwise."""
    loop = asyncio.get_running_loop()
    ceiling = loop.time() + CEILING_S
    backstop, fails = ceiling, 0
    while True:
        if why := _gone(flow, ppage, hooks):
            raise _Stop(why)
        hooks.touch()
        final = loop.time() >= backstop
        try:
            status = await rc.release_status(flow.nonce)
            fails = 0
        except rc.ReleaseClientError as e:
            fails += 1
            if final or fails >= STATUS_FAILS:
                raise _Stop(e.code) from None
            await asyncio.sleep(POLL_S)
            continue
        state = status.get("state")
        if state == "granted":
            return
        if state in ("refused", "expired"):
            raise _Stop("not_approved" if state == "refused" else "release_expired")
        if state != "pending":
            raise _Stop("bad_reply")
        if final:
            raise _Stop("release_timed_out")
        left = status.get("expires_in_s")
        if isinstance(left, int) and not isinstance(left, bool) and left >= 0:
            backstop = min(ceiling, loop.time() + left + 30)
        if report is not None:
            await report(
                "waiting for the owner's approval in Telegram "
                f"({max(0, round(backstop - loop.time()))} s left)"
            )
        await asyncio.sleep(POLL_S)


def _is_challenge(url) -> bool:
    try:
        parts = urlsplit(url)
    except (ValueError, TypeError):
        return False
    host = (parts.hostname or "").lower()
    return bool(
        any(domain_matches(host, h) for h in _CHALLENGE_HOSTS)
        or re.match(r"acs[0-9]*[.-]", host) is not None
        or _CHALLENGE_PATH.search(parts.path or "")
    )


async def _outcome(
    flow: _Flow, ppage, start: str, before: set, popups: int, site: str, hooks: Hooks
) -> str:
    """What followed the Pay click (``start``: the URL before it; ``before``: the
    frame URLs then; ``popups``: the windows opened before it), from navigation, new frame URLs and a bank frame shown. A
    request the network guard saw carrying a card value outranks them all: a redirect hop
    it could only see (leaked) before a request it stopped (blocked)."""
    loop = asyncio.get_running_loop()
    deadline, moved = loop.time() + SETTLE_S, False
    while loop.time() < deadline:
        if flow.url_leaked:
            return "card_url_leaked"
        if flow.url_blocked:
            return "card_url_blocked"
        if not hooks.alive() or ppage.is_closed():
            return "outcome_unknown"
        # ppage.frames includes the main frame: a top-level move to a challenge
        # page (a merchant-hosted /3ds included) is a new URL here too.
        if flow.popups != popups or any(
            _is_challenge(f.url) for f in list(ppage.frames) if f.url not in before
        ):
            return "challenge"
        if not moved and (ppage.url != start or flow.navs):
            moved, deadline = True, loop.time() + NAV_GRACE_S
        hooks.touch()
        await _beat()
        await asyncio.sleep(TICK_S)
    if flow.url_leaked:
        return "card_url_leaked"
    if flow.url_blocked:
        return "card_url_blocked"
    if not moved:
        return _NO_SETTLE  # no settle within the bound: a bank check, a decline, or stuck
    host = urlsplit(ppage.url).hostname
    if not (host and domain_matches(host.lower(), site)):
        return "challenge"
    return "challenge" if await _bank_frame_shown(ppage) else "submitted_unverified"


def _popped(flow: _Flow) -> None:
    flow.popup = True
    flow.popups += 1


def _typing_outcome(flow: _Flow) -> str | None:
    """What happened while the card was typed that outranks a plain stop: the
    card sent in a URL (the exposure is never hidden behind a move), or
    the checkout moving on by itself (a navigation request or a new window)."""
    if flow.url_blocked or flow.url_leaked:  # a short value alone may be a coincidence
        return "card_url_before_pay" if flow.number_hit else "card_value_in_request"
    if flow.typing_marks is not None and (flow.nav_requests, flow.popups) != flow.typing_marks:
        return "moved_while_filling"
    return None


async def _bank_frame_shown(ppage) -> bool:
    """A bank-check frame loaded before Pay (its URL already seen, so not new)
    that is showing now: a checkout may preload one hidden and reveal it. One
    that cannot be asked is not ruled out."""
    for f in list(ppage.frames):
        if f is ppage.main_frame or not _is_challenge(f.url):
            continue
        try:
            if await _step((await _step(f.frame_element())).is_visible()):
                return True
        except Exception:  # noqa: BLE001
            return True
    return False


async def _run(flow: _Flow, args: cf.CardArgs, main_ctx, main_page, hooks: Hooks, report) -> dict:
    url = main_page.url
    main_host = urlsplit(url).hostname
    if cf.origin_of(url) is None or not main_host:
        raise _Stop("not_http")
    flow.host = main_host
    # A host that is itself a public suffix (github.io) is no one site: its
    # "site" would hand over every tenant's cookies and storage.
    if _is_public_suffix(main_host):
        raise _Stop("checkout_on_public_suffix")
    site = registrable_domain(main_host)

    # Handover. Frame chains resolve read-only in the main page first (the origins
    # of every frame on each chain choose the storage handed over: a wrapper frame
    # may need its own to rebuild the inner one); nothing there is filled or clicked.
    if getattr(main_ctx, "browser", None) is None:  # nothing to open a second context with
        raise _Stop("checkout_load_failed")
    frame_origins = set()
    for kind, (frames, _sel) in [*args.fields.items(), ("pay", args.pay)]:
        if frames:
            chain: list = []
            frame = await _walk(main_page.main_frame, frames, chain, wait=True)
            if frame is None or cf.origin_of(frame.url) is None:
                raise _Stop(f"frame_not_found:{kind}")
            frame_origins.update(o for fr in chain if (o := cf.origin_of(fr.url)))
    # storage_state reads origins without a live page through a temporary main-context
    # page that visits each one (answered locally, no network): it grows with history.
    state = await asyncio.wait_for(main_ctx.storage_state(indexed_db=True), EXPORT_S)
    handed = cf.filter_state(state, site, frame_origins)
    del state
    session: dict = {}
    try:  # the main page's own origin only
        got = await _step(main_page.main_frame.evaluate(_SESSION_JS))
        session = (
            {k: v for k, v in got.items() if isinstance(v, str)} if isinstance(got, dict) else {}
        )
    except Exception as e:  # noqa: BLE001 - the checkout may still load without it
        logger.warning("payment sessionStorage read failed (%s)", type(e).__name__)
    try:
        from camoufox.utils import STOCK_MEDIA_DEFAULTS

        pctx = flow.pctx = await _step(
            main_ctx.browser.new_context(
                storage_state=handed,
                no_viewport=True,  # as the persistent launch; a viewport can hang new_page()
                accept_downloads=False,
                service_workers="block",  # a worker's requests would not pass the route
                **STOCK_MEDIA_DEFAULTS,
            )
        )
        del handed
        pctx.on("dialog", lambda d: _spawn(d.dismiss()))
        await _step(pctx.add_init_script(MASK_INIT_JS))
        guard = _UrlGuard(flow)
        await _step(pctx.route("**/*", guard.handle))
        pctx.on("request", guard.on_request)
        seed = None
        if session:  # into an empty sessionStorage of the same origin, before page scripts
            seed = await _step(
                pctx.add_init_script(
                    f"(() => {{ const d = {json.dumps(session)};"
                    f" if (location.origin === new URL({json.dumps(url)}).origin"
                    " && sessionStorage.length === 0)"
                    " for (const [k, v] of Object.entries(d)) sessionStorage.setItem(k, v); })();"
                )
            )
        del session
        ppage = await _step(pctx.new_page())
        guard.main_frame = ppage.main_frame
        # The context's "page" event fires for new_page() too: only another page is a popup.
        pctx.on("page", lambda p: p is not ppage and _popped(flow))
        await asyncio.wait_for(
            ppage.goto(url, wait_until="domcontentloaded", timeout=GOTO_MS), GOTO_MS / 1000 + 5
        )
        if seed is not None:  # the first document only: a later page is not re-seeded
            await _step(seed.dispose())
    except Exception as e:  # noqa: BLE001
        logger.warning("payment context load failed (%s)", type(e).__name__)
        raise _Stop("checkout_load_failed") from None
    if not await _mask_ok(ppage):
        raise _Stop("checkout_load_failed")
    host, top = _top(flow, ppage, site)
    if await hooks.interstitial(ppage):
        raise _Stop("bot_check")

    fields, pay, label = await _resolve(ppage, args, wait=True)
    origins = {k: f.origin for k, f in fields.items()}
    seen = {**origins, "pay": pay.origin}
    await _extras(ppage, args, fields, fill=True)  # before the tap: no card value exists yet
    if not await _mask_ok(ppage):
        raise _Stop("mask_unverified")
    # After the extras (an address can re-price), and the total may render late.
    until = asyncio.get_running_loop().time() + ATTACH_S
    while not await _amount_ok(ppage, args.amount):
        if asyncio.get_running_loop().time() >= until:
            raise _Stop("amount_not_on_page")
        await asyncio.sleep(POLL_S)
    # What the tap shows is read again now: filling the extras can re-render the
    # checkout (an address re-pricing it, or moving it to another host of the
    # site), and the owner approves what it shows.
    host, top = _top(flow, ppage, site)
    fields, pay, label = await _resolve(ppage, args)
    origins = {k: f.origin for k, f in fields.items()}
    seen = {**origins, "pay": pay.origin}

    # The tap.
    title = ""
    with contextlib.suppress(Exception):
        title = await _title(ppage)
    secret = secrets.token_hex(32)
    request = asyncio.ensure_future(
        rc.request_release(
            args.card_ref,
            host,
            title,
            args.amount,
            hashlib.sha256(secret.encode()).hexdigest(),
            origins,
            label,
            list(args.extra),
        )
    )
    try:
        reply = await asyncio.shield(request)
    except rc.ReleaseClientError as e:
        raise _Stop(e.code) from None
    except asyncio.CancelledError:
        # The vault may have made the release before the cancel landed. Wait
        # briefly for its reply so teardown can cancel the pending tap (and free
        # the vault's one release slot) instead of leaving it to expire.
        with contextlib.suppress(BaseException):
            late = await asyncio.wait_for(asyncio.shield(request), timeout=CANCEL_S)
            nonce = late.get("nonce") if isinstance(late, dict) else None
            if isinstance(nonce, str) and _NONCE.fullmatch(nonce):
                flow.nonce = nonce
        if not request.done():
            request.cancel()
        raise
    if reply.get("ok") is not True:
        if reply.get("error") == "the approval text was refused":
            raise _Stop("approval_text_refused")
        raise _Stop("release_refused", reason=reply.get("error"))
    if not isinstance(reply.get("nonce"), str) or not _NONCE.fullmatch(reply["nonce"]):
        raise _Stop("bad_reply")
    flow.nonce = reply["nonce"]
    await _await_grant(flow, ppage, hooks, report)
    flow.granted = True

    # Granted: the page must still show what the tap was for, fields re-resolved.
    fields, pay, _label = await _resolve(ppage, args)
    if not await _same_page(ppage, args, top, seen, label, title, fields, pay, hooks):
        raise _Stop("page_changed")
    if why := _gone(flow, ppage, hooks):
        raise _Stop(why)
    flow.claimed = True  # from here the vault may hand the card over: no cancel
    await _beat()  # the claim may take up to 90 s
    try:
        reply = await rc.claim(flow.nonce, secret)
    except rc.ReleaseClientError:
        raise _Stop("claim_unanswered") from None
    finally:
        del secret
    card = reply.pop("card", None)
    if reply.get("ok") is not True or not isinstance(card, dict):
        del card
        raise _Stop("claim_refused", reason=reply.get("error"))
    try:
        if set(card) != cf.reply_keys(fields):
            raise _Stop("claimed_not_filled")  # the vault and the tool disagree about the tap
        values = cf.values_for(card, list(fields), args.expiry_format)
    finally:
        del card
    guard.arm(values)  # before anything is typed

    # Typing. Nothing is typed into a page that changed after the tap.
    if _gone(flow, ppage, hooks) or not await _same_page(
        ppage, args, top, seen, label, title, fields, pay, hooks
    ):
        values.clear()
        raise _Stop("claimed_not_filled")
    # From the first keystroke: a field's input handler can submit the checkout
    # itself, and that is no "nothing paid" stop.
    flow.typing_marks = (flow.nav_requests, flow.popups)
    try:
        flow.typing = True
        for kind, f in fields.items():
            if flow.url_blocked or flow.url_leaked:
                raise _Stop(_typing_outcome(flow), filled=flow.typed)
            if cf.origin_of(f.frame.url) != origins[kind] or not await _mask_ok(ppage):
                raise _Stop("failed_before_pay", field=kind, filled=flow.typed)
            if f.tag == "select":
                await _select(f.handle, values[kind], kind)
                got = await _step(f.handle.evaluate(EXTRA_VALUE_JS))
                if isinstance(got, list) and got and isinstance(got[0], str):
                    guard.add_option(kind, got[0])
            else:
                await _step(f.handle.fill(values[kind], timeout=10000))
            if not await _holds(f, kind, values[kind]):
                raise _Stop("failed_before_pay", field=kind, filled=[*flow.typed, kind])
            flow.typed.append(kind)

        # Pay. The one final re-check of the page as tapped, then every card field
        # still holding what was typed (a later field's input, or a script on the
        # way to Pay, can clear or rewrite an earlier one). The page owns its DOM
        # between this and the click: a script changing what the tap showed in
        # that window is an accepted residual (owner ruling 07a1c756), and where
        # a card value can go in a URL is the network guard's job, not the DOM's.
        if not await _same_page(
            ppage, args, top, seen, label, title, fields, pay, hooks, clicking=True
        ):
            raise _Stop("failed_before_pay", filled=flow.typed)
        for kind, f in fields.items():
            if cf.origin_of(f.frame.url) != origins[kind] or not await _holds(
                f, kind, values[kind]
            ):
                raise _Stop("failed_before_pay", field=kind, filled=flow.typed)
        # After the last await: nothing below lets a route or page event be handled
        # before the click, so these are final for all the browser has reported.
        if code := _typing_outcome(flow):
            raise _Stop(code, filled=flow.typed)
    except Exception:
        if code := _typing_outcome(flow):  # whatever stopped it, this says more
            raise _Stop(code, filled=flow.typed) from None
        raise
    finally:
        values.clear()
    ppage.on(
        "framenavigated", lambda fr: fr is ppage.main_frame and setattr(flow, "navs", flow.navs + 1)
    )
    start, before, popups = ppage.url, {f.url for f in list(ppage.frames)}, flow.popups
    flow.paid = True  # set before the click: a click that raised may still have landed
    click_raised = False
    try:
        await _step(pay.handle.click(timeout=CLICK_MS))
    except Exception as e:  # noqa: BLE001 - never the exception's text
        click_raised = True
        logger.warning("payment Pay click raised (%s)", type(e).__name__)
    code = await _outcome(flow, ppage, start, before, popups, site, hooks)
    if code == _NO_SETTLE:
        # Nothing moved on. After a clean click that is a bank check or a stuck
        # checkout; after a click that raised, not even the click is known.
        code = "pay_click_unknown" if click_raised else "challenge"
    # A flag set after _outcome's last look still outranks what it saw.
    if flow.url_leaked:
        code = "card_url_leaked"
    elif flow.url_blocked:
        code = "card_url_blocked"
    return cf.result(code, flow.host, filled=flow.typed)
