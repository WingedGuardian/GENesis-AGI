"""The payment-tab guard: while a tab holds a filled card, no tool reads it.

A Camoufox tab the card-fill flow (#3172) fills is *armed* from the moment it
is chosen until it closes. Arming is keyed by the Playwright ``Page``, never by
host or timer, and the browser tools (``browser.py``) consult it:

* run_js is refused in the whole browser while any tab is armed, or while a
  closed payment tab's site storage is still being cleared;
* the armed tab refuses screenshot, fill, upload and navigate, and press_key
  takes only the keys in :data:`ALLOWED_KEYS`;
* every page snapshot goes through :meth:`PaymentTabs.redact_snapshot`;
* ``_ts_log`` writes nothing and ``CARD_FILTER`` withholds log arguments and
  hides filled values in the format string it keeps.

Back restores filled values from session history in every pref configuration
(measured), so the flow ends by CLOSING the tab (:meth:`PaymentTabs.finish`),
which also clears the site storage of the origins the tab showed.

The *taint*: any run_js, or any non-http(s) navigation, since this Camoufox
launch taints the browser context, and arming then refuses with
``browser_tainted_restart``. A script planted in another tab can reach an armed
tab through ``window.opener`` or the handle ``window.open`` returned, so a
per-page check is not enough. Only a Camoufox restart clears it.

Filled values are never kept. :meth:`PaymentTabs.begin_fill` stores HMACs of
them, under a key made for that arming, before the first one is typed, and
snapshot tokens whose HMAC matches are hidden.

The origins an armed tab shows are written to a 0600 file
(:func:`genesis.env.payment_tab_pending_path`) from arm until their site
storage is cleared, so an MCP crash or kill leaves them for the next Camoufox
launch to clear. run_js stays refused until it has.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import fcntl
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import unicodedata
import weakref
from dataclasses import dataclass, field
from urllib.parse import unquote_plus, urlsplit

import yaml

from genesis import env
from genesis.browser.types import BrowserLayer
from genesis.security.card_redaction import CARD_FILTER
from genesis.security.card_redaction import redact_text as redact_cards

logger = logging.getLogger(__name__)

HIDDEN = "[hidden: payment tab]"

# Pinned at launch while card fill is enabled. The first six keep form data
# out of form history, autofill and the session store; max_tabs_undo=0 makes a
# closed payment tab unrestorable, and clipboard.autocopy=false stops X11
# primary-selection copies of selected text. The rest pin what Camoufox only
# defaults to, so an engine update cannot turn it back on: no crash restore of
# the session, no password capture (a CVC field can be type=password), and no
# history or disk cache, whose keys are URLs (a GET form puts the filled
# values in its URL).
CARD_FILL_PREFS = {
    "browser.formfill.enable": False,
    "signon.autofillForms": False,
    "extensions.formautofill.creditCards.enabled": False,
    "extensions.formautofill.addresses.enabled": False,
    "browser.sessionstore.privacy_level": 2,
    "browser.sessionhistory.max_total_viewers": 0,
    "browser.sessionstore.max_tabs_undo": 0,
    "clipboard.autocopy": False,
    "browser.sessionstore.resume_from_crash": False,
    "signon.rememberSignons": False,
    "signon.formlessCapture.enabled": False,
    "places.history.enabled": False,
    "browser.cache.disk.enable": False,
}

# Keys browser_press_key sends to an armed tab. MEASURED in Camoufox, each key
# pressed in a focused text, tel, number, search, select, listbox, textarea,
# checkbox and range control:
#   Tab, Shift+Tab: move focus, change no value.
#   Enter: submits; inserts a newline in a textarea, changes no card control.
#   Escape: empties a type=search input, changes no card control.
# Refused, because each changes a card control: Space (a text value, a
# checkbox), ArrowUp/ArrowDown (a number, a select), and ArrowLeft/ArrowRight,
# PageUp/PageDown, Home and End (a select, which is how an expiry month is
# often picked). A snapshot reads the whole page, so no scroll key is needed.
ALLOWED_KEYS = frozenset({"Enter", "Tab", "Shift+Tab", "Escape"})


def card_fill_enabled() -> bool:
    """``card_fill.enabled`` in ``config/payments.yaml`` (plus its ``.local.yaml``
    overlay). Off unless it is exactly ``true``; a missing or unreadable file
    means off, and ``GENESIS_CARD_FILL_DISABLED=1`` turns it off whatever the
    file says. Read on every call."""
    if os.environ.get("GENESIS_CARD_FILL_DISABLED") == "1":
        return False
    from genesis._config_overlay import merge_local_overlay
    from genesis.env import repo_root

    path = repo_root() / "config" / "payments.yaml"
    try:
        base = yaml.safe_load(path.read_text()) if path.is_file() else {}
        cfg = merge_local_overlay(base if isinstance(base, dict) else {}, path, strict=True)
    except Exception as e:
        logger.warning("payments config unreadable (%s): card fill is off", type(e).__name__)
        return False
    section = cfg.get("card_fill") if isinstance(cfg, dict) else None
    return isinstance(section, dict) and section.get("enabled") is True


class State(enum.Enum):
    AWAITING_TAP = "awaiting_tap"  # chosen; nothing filled yet
    FILLING = "filling"
    ARMED = "armed"  # filled
    CLOSING = "closing"  # finish has begun


class PaymentTabError(Exception):
    """A refused arming call. ``code`` is one of: ``other_payment_tab``,
    ``browser_tainted_restart``, ``prefs_not_pinned`` (the browser was not
    launched with :data:`CARD_FILL_PREFS`), ``pending_store_failed`` (the
    pending-origins file cannot be written), ``mask_failed`` (the mask for
    later documents could not be installed), ``mask_unverified`` (begin_fill
    on a tab whose mask did not verify), ``page_busy`` (a tool action on the
    page did not end in time), ``already_armed``, ``no_payment_tab``,
    ``wrong_state``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# The mask: an armed tab draws nothing of its document, so neither a
# screenshot nor the VNC view shows a value. A value is not only drawn by a
# form control: a card-preview widget mirrors it into plain text, a canvas or
# an SVG; an input can sit in a shadow root, which no document stylesheet
# reaches. So the root element of every frame is made fully transparent: its
# whole subtree (shadow trees, iframes, pseudo-elements, canvas) composites
# with it. Opacity changes no layout or hit testing, and Playwright counts an
# opacity-0 element as visible, so clicks still land. The control rules stay
# as a second layer. Set as a rule AND inline (an inline !important beats a
# page rule), applied in every frame, again on each new document (Back builds
# one), and checked by _MASK_CHECK_JS.
MASK_CSS = (
    ":root { opacity: 0 !important }"
    " input, textarea, select, [contenteditable] {"
    " color: transparent !important; -webkit-text-fill-color: transparent !important;"
    " caret-color: transparent !important; text-shadow: none !important }"
    " ::selection { color: transparent !important; background: transparent !important }"
)
_MASK_JS = (
    "() => { const id = '__genesis_payment_mask__';"
    " const root = document.documentElement;"
    " if (!root) return false;"
    " root.style.setProperty('opacity', '0', 'important');"
    " let s = document.getElementById(id);"
    " if (!s) { s = document.createElement('style'); s.id = id;"
    " (document.head || root).appendChild(s); }"
    " s.textContent = '" + MASK_CSS + "'; return true; }"
)
# Runs before the page's own scripts in each new document of the armed tab,
# so a Back-restored form is masked before it is first painted.
_MASK_INIT_JS = (
    "(() => { const m = " + _MASK_JS + ";"
    " if (!m()) { const o = new MutationObserver(() => { if (m()) o.disconnect(); });"
    " o.observe(document, {childList: true, subtree: true}); }"
    " document.addEventListener('DOMContentLoaded', m); })();"
)
_MASK_CHECK_JS = """() => {
  const clear = (v) => v === 'transparent' || /^rgba\\([^)]*,\\s*0\\)$/.test(v);
  // The root's opacity is what hides text, canvas and shadow trees: the
  // controls below are only those a document query can see.
  const root = document.documentElement;
  if (!root || getComputedStyle(root).opacity !== '0') return false;
  for (const e of document.querySelectorAll('input, textarea, select')) {
    const s = getComputedStyle(e);
    if (!clear(s.color) || !clear(s.webkitTextFillColor) || !clear(s.caretColor)) return false;
    if (s.textShadow && s.textShadow !== 'none') return false;
  }
  return true;
}"""
# Clears the frame's origin storage (local, session, IndexedDB, Cache API,
# the origin-private file system) and unregisters its service workers.
# Cookies are left: they hold the site login, and no browser stores form
# values in them.
_CLEAR_STORAGE_JS = """async () => {
  let ok = true;
  try { localStorage.clear(); } catch (e) { ok = false; }
  try { sessionStorage.clear(); } catch (e) { ok = false; }
  try {
    // Only success counts. "blocked" is a notice, not an end: another
    // connection holds the database, and the same request still succeeds once
    // it closes. A delete still blocked after 5 s (the clear runs on a page
    // with no network, so nothing else should hold it longer) fails, leaving
    // the origin pending. A browser that cannot list its databases cannot be
    // cleared.
    if (!indexedDB.databases) ok = false;
    else {
      for (const d of await indexedDB.databases()) {
        const deleted = await new Promise((r) => {
          const q = indexedDB.deleteDatabase(d.name);
          const t = setTimeout(() => r(false), 5000);
          q.onsuccess = () => { clearTimeout(t); r(true); };
          q.onerror = () => { clearTimeout(t); r(false); };
        });
        if (!deleted) ok = false;
      }
    }
  } catch (e) { ok = false; }
  try {
    if (self.caches) for (const k of await caches.keys()) await caches.delete(k);
  } catch (e) { ok = false; }
  try {
    // The origin-private file system is its own bucket. A browser without it
    // exposes no getDirectory and so holds nothing there; one that has it and
    // fails to list or delete leaves the origin pending.
    if (navigator.storage && navigator.storage.getDirectory) {
      const root = await navigator.storage.getDirectory();
      // Stepped by hand: in Camoufox's evaluation world the handle's key
      // iterator has next() but no async-iterator protocol (MEASURED:
      // for-await throws "not iterable", next() lists every entry).
      const names = [];
      const keys = root.keys();
      for (;;) {
        const step = await keys.next();
        if (step.done) break;
        names.push(step.value);
      }
      for (const name of names) await root.removeEntry(name, { recursive: true });
    }
  } catch (e) { ok = false; }
  try {
    // A service worker answers this origin's requests before the browser's
    // routing sees them, and can hold the data above open: unregistered.
    if (navigator.serviceWorker && navigator.serviceWorker.getRegistrations) {
      for (const r of await navigator.serviceWorker.getRegistrations()) {
        if (!(await r.unregister())) ok = false;
      }
    }
  } catch (e) { ok = false; }
  return ok;
}"""
_CLEAR_PATH = "/__genesis_payment_tab_clear__"
_CLEAR_TIMEOUT_MS = 10000  # a fulfilled, network-free document loads in milliseconds
# How long arm() waits for tool actions already running on the page. A key
# press, click or upload ends within its 60 s tool timeout; a navigate can run
# 300 s (challenge handling) and a fill has no deadline. Running out refuses
# the arming (page_busy) and changes nothing, so the flow can retry; waiting
# out a full navigate would only hold the flow at the moment it was chosen.
_ARM_IDLE_WAIT_S = 60.0

# One aria-snapshot line of a value-bearing control: indent, role, then the
# quoted name and [attributes], then ": value" (or ":" before children).
# Playwright YAML-quotes the whole key in single quotes (a quote inside it
# doubled) when the name holds ": ", " #", braces or a backtick
# (yamlEscapeKeyIfNeeded in its coreBundle.js): - 'textbox "Expiry: MM / YY"': 12/30
_CONTROL_ROLES = ("textbox", "searchbox", "spinbutton", "combobox")
_ROLE = r"(?:" + "|".join(_CONTROL_ROLES) + r")\b"
_CONTROL_LINE = re.compile(
    r"^(?P<head>(?P<indent>\s*)- (?:'" + _ROLE + r"(?:[^']|'')*'"
    r"|" + _ROLE + r'(?: "(?:[^"\\]|\\.)*")?(?: \[[^\]\n]*\])*))(?P<rest>.*)$'
)
_CONTROL_START = re.compile(r"^\s*- '?" + _ROLE)
_OPTION_START = re.compile(r"^\s*- '?option\b")
_TEXT_TOKEN = re.compile(r"[^\s\"'`<>()\[\]{}:;,|]+")
_ALNUM_RUN = re.compile(r"[^\W_]+")  # letters and digits, any script
# What a page puts between the parts of a value it reformats: any space,
# slashes, dashes, dots, apostrophes and the invisible joiners, in ASCII,
# Unicode and full-width forms (``12 / 34``, ``7 3 9``, ``SW1A 1AA``). A run of
# letter-or-digit parts joined by up to four of them is matched as the
# concatenation of any consecutive parts (:meth:`PaymentTabs._hide_spans`).
_SEP = r"[\s/.\-_'\u2010-\u2015\u2018\u2019\u2212\u200b-\u200d\u2060\ufeff\uff0d\uff0e\uff0f]"
_SPAN = re.compile(r"[^\W_]+(?:" + _SEP + r"{1,4}[^\W_]+)+")
# An expiry as a fill sends it: month, a separator, a two- or four-digit year.
_EXPIRY = re.compile(r"(\d{1,2})\s*[/\-.]\s*(\d{2}|\d{4})")


def _nfkc(text: str) -> str:
    """One spelling per character: composed accents, ASCII for full-width forms."""
    return unicodedata.normalize("NFKC", text)


def _fold(text: str) -> str:
    """The form a value part is registered and compared in: NFKC, accents
    dropped (a card-preview widget prints ``JOSE`` for ``José``), casefolded."""
    bare = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return _nfkc(bare).casefold()


_MONTHS = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)  # fmt: skip


def _expiry_forms(value: str) -> set[str]:
    """Each run-together spelling a page may show an expiry ``value`` in:
    month as digits (with and without its leading zero) or as an English name
    (full and three-letter), year in two and four digits, month first and
    year first. Empty when ``value`` is not an expiry."""
    m = _EXPIRY.fullmatch(value)
    if m is None or not 1 <= int(m.group(1)) <= 12:
        return set()
    month, year = int(m.group(1)), m.group(2)
    name = _MONTHS[month - 1]
    years = {year[-2:], year if len(year) == 4 else "20" + year}
    months = {f"{month:02d}", str(month), name, name[:3]}
    return {mo + y for mo in months for y in years} | {y + mo for mo in months for y in years}


_PREFIX_MIN = 6  # a card's issuer prefix: shorter prefixes match too much


def _card_prefixes(digits: str) -> set[str]:
    """The prefixes of a card number a live preview shows while it is typed
    (``4242 4242 42``): from :data:`_PREFIX_MIN` digits up. Empty for a value
    too short to be a card number."""
    if len(digits) < 12:
        return set()
    return {digits[:n] for n in range(_PREFIX_MIN, len(digits))}


_MIN_PART = 3  # shorter value parts are not matched: too common to redact on sight


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def hide_form_values(text: str) -> str:
    """The form-value step of the armed-snapshot redaction: every textbox, searchbox,
    spinbutton and combobox value becomes :data:`HIDDEN` (with whatever is
    nested under it), and every option is dropped with its children. A line
    whose shape is not recognised keeps only its role."""
    out: list[str] = []
    skip_deeper: int | None = None
    for line in text.splitlines():
        if skip_deeper is not None:
            if line.strip() and _indent(line) > skip_deeper:
                continue
            skip_deeper = None
        if _OPTION_START.match(line):
            skip_deeper = _indent(line)
            continue
        if _CONTROL_START.match(line):
            skip_deeper = _indent(line)
            m = _CONTROL_LINE.match(line)
            if m is None or (m.group("rest") and not m.group("rest").startswith(":")):
                role = line.strip()[2:].lstrip("'").split(" ", 1)[0].split(":", 1)[0]
                out.append(f"{' ' * _indent(line)}- {role}: {HIDDEN}")
            else:
                out.append(f"{m.group('head')}: {HIDDEN}")
            continue
        out.append(line)
    return "\n".join(out)


def origin_of(url: str) -> str | None:
    """``scheme://host[:port]`` of an http(s) URL, else None."""
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except (ValueError, TypeError):
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    host = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{host}" + (f":{port}" if port else "")


def _frame_origins(page) -> set[str]:
    urls = []
    with contextlib.suppress(Exception):
        urls.append(page.url)
    with contextlib.suppress(Exception):
        urls.extend(f.url for f in page.frames)
    return {o for u in urls if isinstance(u, str) and (o := origin_of(u))}


@dataclass(eq=False)
class _Arming:
    layer: BrowserLayer
    state: State = State.AWAITING_TAP
    key: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    digests: set[bytes] = field(default_factory=set)
    lengths: set[int] = field(default_factory=set)
    origins: set[str] = field(default_factory=set)
    crashed: bool = False
    masked: bool = False  # the last verify_mask passed
    page: object = None  # the armed tab (its popups share this arming)
    doc_gen: int = 0  # bumped by each new document, so a stale mask check is discarded


class PaymentTabs:
    """The arming state of one MCP process. :data:`TABS` is the instance."""

    def __init__(self) -> None:
        # Set by browser.py: moves the tools off a page that is about to close.
        self.move_off = None
        self.reset()

    def reset(self) -> None:
        """Forget everything (tests, and the MCP exit)."""
        self._tabs: dict = {}
        self._popups: dict = {}  # a page an armed tab (or its popup) opened -> that arming
        self._pending: dict[str, BrowserLayer] = {}  # origin -> layer, storage not yet cleared
        self._clears = weakref.WeakKeyDictionary()  # closed page -> its storage-clear task
        self._tasks: set[asyncio.Task] = set()
        self._listening = weakref.WeakSet()  # pages whose event handlers are registered
        self._tainted: str | None = None
        self._written: frozenset[str] = frozenset()  # origins in the pending file
        self._store_broken = False  # the pending file could not be read
        self._acting: dict = {}  # page -> browser-tool actions running on it (acting)
        self._idle: dict = {}  # page -> Event set when its last action ends (arm waits)
        self.prefs_pinned = False
        self.epoch = 0  # bumped by every arm, so a read can tell one began meanwhile
        self.finished = 0  # bumped by every armed tab's close: its keyed hashes go with it

    # --- reads -----------------------------------------------------------
    def any_armed(self) -> bool:
        return bool(self._tabs or self._popups)

    def is_armed(self, page) -> bool:
        """``page`` is an armed tab, or a popup one opened (it can show the
        filled values: a same-origin popup gets a copy of sessionStorage, and
        a wallet or 3-D Secure window echoes them), so every guard holds on it."""
        return page is not None and (page in self._tabs or page in self._popups)

    def _entry_of(self, page) -> _Arming | None:
        if page is None:
            return None
        return self._tabs.get(page) or self._popups.get(page)

    def crashed(self, page) -> bool:
        entry = self._entry_of(page)
        return entry is not None and entry.crashed

    def state(self, page) -> State | None:
        entry = self._entry_of(page)
        return None if entry is None else entry.state

    def store_broken(self) -> bool:
        """The pending file could not be read (run_js and arming refused)."""
        return self._store_broken

    def shows_pending(self, page) -> bool:
        """``page`` (a Camoufox page) may render site storage a payment tab
        left: one of its frames is at an origin whose clear has not succeeded
        (still running, failed, or left by a crash), or the pending file
        could not be read, so any origin may be one. Its content and pixels
        are not shown until a clear succeeds."""
        if self._store_broken:
            return True
        return bool(self._pending) and bool(_frame_origins(page) & self._pending.keys())

    def profile_may_hold_card_data(self) -> bool:
        """Before a Camoufox launch: the profile may hold site storage a
        payment tab showed and no clear has removed (an origin pending here or
        in the pending file, an armed tab, or a file that cannot be read). A
        profile backup made now would keep that storage past the clear."""
        if self._on_disk() or self._store_broken:
            return True
        path = env.payment_tab_pending_path()
        try:
            with _locked(path):
                disk = _read_pending(path)
        except OSError:
            return True
        return disk is None or bool(disk)

    def run_js_blocked(self) -> bool:
        """True while a tab is armed, a closed one's storage is not yet
        cleared, or the pending file could not be read."""
        return bool(self._tabs or self._popups or self._pending or self._store_broken)

    # --- taint -----------------------------------------------------------
    def taint(self, reason: str) -> None:
        if self._tainted is None:
            logger.info("browser context tainted for card fill: %s", reason)
            self._tainted = reason

    def tainted(self) -> bool:
        return self._tainted is not None

    def on_camoufox_closed(self) -> None:
        """Camoufox's context is gone: its taint and launch prefs go with it."""
        self._tainted = None
        self.prefs_pinned = False

    # --- actions in flight -------------------------------------------------
    @contextlib.contextmanager
    def acting(self, page):
        """Held by a browser tool for as long as it acts on ``page`` (types,
        clicks, presses, navigates, runs a script, captures). The tool enters
        it BEFORE its arming check, with no await between the two: an action
        that began unarmed is then one :meth:`arm` waits for, and one that
        begins after the arming refuses."""
        self._acting[page] = self._acting.get(page, 0) + 1
        try:
            yield
        finally:
            left = self._acting.get(page, 1) - 1
            if left > 0:
                self._acting[page] = left
            else:
                self._acting.pop(page, None)
                idle = self._idle.pop(page, None)
                if idle is not None:
                    idle.set()

    async def _wait_idle(self, page) -> bool:
        """Wait, up to :data:`_ARM_IDLE_WAIT_S`, until no action is running
        on ``page``. False when one still is."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + _ARM_IDLE_WAIT_S
        while page in self._acting:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            idle = self._idle.setdefault(page, asyncio.Event())
            try:
                await asyncio.wait_for(idle.wait(), timeout=remaining)
            except TimeoutError:
                return False
        return True

    # --- arming ----------------------------------------------------------
    async def arm(self, page, layer: BrowserLayer = BrowserLayer.CAMOUFOX) -> bool:
        """Arm ``page`` (AWAITING_TAP) and mask it. Returns whether the mask
        verified in every frame. Raises :class:`PaymentTabError`.

        A tool action already running on the page (a fill, key press or
        navigation that passed its arming check before this call) is waited
        for: none acts on the page once this returns. One still running after
        :data:`_ARM_IDLE_WAIT_S` refuses the arming (``page_busy``). So the
        caller must not hold an action on the page itself."""
        if page in self._tabs:
            raise PaymentTabError("already_armed")
        if self._tabs:
            raise PaymentTabError("other_payment_tab")
        if self._tainted is not None:
            raise PaymentTabError("browser_tainted_restart")
        # Only a Camoufox launched while card fill was on has the prefs that
        # keep form data out of form history and the session store.
        if layer is not BrowserLayer.CAMOUFOX or not self.prefs_pinned:
            raise PaymentTabError("prefs_not_pinned")
        # Read again here, not only at launch: the kill switch stops a flow
        # in a browser launched while card fill was on.
        if not card_fill_enabled():
            raise PaymentTabError("card_fill_off")
        # Registered before the first await: every guard holds from here on.
        entry = self._tabs[page] = _Arming(layer=layer, page=page)
        self.epoch += 1
        entry.origins |= _frame_origins(page)
        if not self._persist():  # a crash would leave its origins uncleared
            self._release(page, entry)
            raise PaymentTabError("pending_store_failed")
        if page not in self._listening:  # each handler does nothing once disarmed
            self._listening.add(page)
            page.once("close", lambda _p: self._on_close(page))
            page.on("crash", lambda _p: self._on_crash(page))
            page.on("framenavigated", lambda frame: self._remask(page, frame))
            page.on("load", lambda _p: self._remask(page, None))
            page.on("popup", lambda popup: self._adopt(page, popup))
        # New actions now refuse; wait out the ones already running, so the
        # mask below lands on the document they leave.
        if not await self._wait_idle(page):
            if self._tabs.get(page) is entry and entry.state is State.AWAITING_TAP:
                self._release(page, entry)
            raise PaymentTabError("page_busy")
        try:
            await page.add_init_script(_MASK_INIT_JS)
        except Exception as e:
            # Without it a document the tab loads later (Back builds one) is
            # painted unmasked. Nothing is filled yet, so the tab is released.
            logger.warning("payment tab mask init script failed: %s", type(e).__name__)
            if self._tabs.get(page) is entry and entry.state is State.AWAITING_TAP:
                self._release(page, entry)
            raise PaymentTabError("mask_failed") from None
        await self.apply_mask(page)
        return await self.verify_mask(page)

    def begin_fill(self, page, values_for_hmac) -> None:
        """Move to FILLING, recording HMACs of the values about to be filled
        BEFORE any is typed: a fill that fails part-way never reaches
        :meth:`mark_filled`, and what it typed must already be hidden."""
        entry = self._require(page)
        if entry.state is not State.AWAITING_TAP:
            raise PaymentTabError("wrong_state")
        if not entry.masked:  # arm(), or the last verify_mask, did not verify
            raise PaymentTabError("mask_unverified")
        entry.origins |= _frame_origins(page)
        if not self._persist():  # an origin a crash would leave uncleared
            raise PaymentTabError("pending_store_failed")
        self._register(entry, values_for_hmac)
        entry.state = State.FILLING

    def mark_filled(self, page, values_for_hmac=()) -> None:
        """Move to ARMED, recording HMACs of any further values (a read-back
        that differs from what was sent). Only after :meth:`begin_fill`,
        which checks the mask and the pending file."""
        entry = self._require(page)
        if entry.state not in (State.FILLING, State.ARMED):
            raise PaymentTabError("wrong_state")
        self._register(entry, values_for_hmac)
        entry.origins |= _frame_origins(page)
        entry.state = State.ARMED
        if not self._persist_or_close(entry):
            raise PaymentTabError("pending_store_failed")

    def _persist_or_close(self, entry: _Arming) -> bool:
        """:meth:`_persist`, failing closed once values may be typed: a crash
        would leave an origin off disk with its storage uncleared, so the tab
        is finished now (closed, its storage cleared) instead of going on
        without crash coverage. Before a fill nothing is at stake, and
        :meth:`begin_fill` writes the file again before the first value."""
        if self._persist():
            return True
        page = entry.page
        if entry.state in (State.FILLING, State.ARMED) and self._tabs.get(page) is entry:
            logger.warning("payment tab pending file not written after a fill: closing the tab")
            self._spawn(self._finish_quietly(page))
        return False

    async def _finish_quietly(self, page) -> None:
        with contextlib.suppress(PaymentTabError):  # a finish already running
            await self.finish(page)

    def _register(self, entry: _Arming, values) -> None:
        """Record HMACs of ``values`` (never the values). Each is matched
        whole, as digits only, as its letters and digits run together (what
        :meth:`_hide_spans` compares a respaced value with), by its
        whitespace-separated parts, and by the letter-or-digit runs of those
        parts (``Brien`` of ``O'Brien``, which :data:`_TEXT_TOKEN` splits at
        the apostrophe), each of at least :data:`_MIN_PART` characters. An
        expiry also registers its other spellings (``12/2034``, ``1/34``,
        ``Dec 2034``), and a card number its prefixes (a preview mid-typing).
        Parts are folded (:func:`_fold`), as are the candidates :meth:`_matches`
        checks, so ``PAT TESTER``, ``JOSE`` for ``José``, a decomposed accent
        and full-width letters match too."""
        for value in values:
            v = _nfkc(str(value)).strip()
            digits = "".join(c for c in v if c.isdigit())
            compact = "".join(_ALNUM_RUN.findall(v))
            for part in {
                v,
                digits,
                compact,
                *_expiry_forms(v),
                *_card_prefixes(digits),
                *v.split(),
                *_ALNUM_RUN.findall(v),
            }:
                part = _fold(part)
                if len(part) >= _MIN_PART:
                    entry.digests.add(self._mac(entry.key, part))
                    entry.lengths.add(len(part))

    def disarm(self, page) -> None:
        """Release a tab nothing was filled into (AWAITING_TAP). A filled tab
        must be closed with :meth:`finish`."""
        entry = self._require(page)
        if entry.state is not State.AWAITING_TAP:
            raise PaymentTabError("wrong_state")
        # Its listeners do nothing once it is not in _tabs. The mask init script
        # cannot be removed (Playwright has no API for it), so the tab's later
        # documents stay masked on screen.
        self._release(page, entry)

    def _release(self, page, entry: _Arming) -> None:
        """Drop an arming nothing was filled into, and its origins from the
        pending file (unless a pending clear still needs them)."""
        if self._tabs.get(page) is entry:
            del self._tabs[page]
        for popup in self._popups_of(entry):  # opened before any fill: nothing in them
            del self._popups[popup]
        self._persist()

    def _popups_of(self, entry: _Arming) -> list:
        return [p for p, e in self._popups.items() if e is entry]

    async def _close_popups(self, entry: _Arming) -> None:
        """Close the pages the armed tab opened (the tools moved off each
        first). One that will not close stays armed."""
        for popup in self._popups_of(entry):
            try:
                if self.move_off is not None:
                    await self.move_off(popup)
                await popup.close(run_before_unload=False)
            except Exception as e:
                logger.warning("payment tab popup close failed: %s", type(e).__name__)

    async def finish(self, page) -> dict:
        """Close the armed tab and clear its origins' site storage.

        The tools are moved off it first (:attr:`move_off`; it opens a fresh
        page when this is the one the tools drive or the context's last).
        run_js stays refused until the storage is cleared. A tab that would
        not close stays armed, and the result says ``closed: False``.
        """
        entry = self._require(page)
        if entry.state is State.CLOSING:
            raise PaymentTabError("wrong_state")  # a finish is already running
        prior, entry.state = entry.state, State.CLOSING
        entry.origins |= _frame_origins(page)
        self._persist()
        try:
            await self._close_popups(entry)
            try:
                if self.move_off is not None:
                    await self.move_off(page)
                elif len(page.context.pages) <= 1:
                    await page.context.new_page()
            except Exception as e:
                logger.warning("payment tab: no page to move to (%s)", type(e).__name__)
            try:
                await page.close(run_before_unload=False)
            except Exception as e:
                logger.warning("payment tab close failed: %s", type(e).__name__)
        finally:
            # A close that failed or was cancelled: still armed, and back in
            # its state, so a later finish (or the MCP exit) can try again.
            if not is_closed(page) and self._tabs.get(page) is entry:
                entry.state = prior
        if not is_closed(page):
            return {"closed": False, "storage_cleared": False}
        self._on_close(page)  # no-op when the close event already ran
        task = self._clears.pop(page, None)
        cleared = await asyncio.shield(task) if task is not None else True
        return {"closed": True, "storage_cleared": bool(cleared)}

    def forget_layer(self, layer: BrowserLayer) -> None:
        """The layer's browser is gone, and so are its tabs. Camoufox keeps its
        profile, so the origins its armed or closed payment tabs showed stay
        pending (run_js refused) until :meth:`clear_pending` runs on the next
        launch. Other layers' pending origins are dropped with them."""
        gone = [p for p, e in self._tabs.items() if e.layer is layer]
        gone += [p for p, e in self._popups.items() if e.layer is layer]
        for page in gone:
            entry = self._tabs.pop(page, None) or self._popups.pop(page)
            if layer is BrowserLayer.CAMOUFOX:
                for origin in entry.origins:
                    self._pending[origin] = layer
        if layer is not BrowserLayer.CAMOUFOX:
            for origin in [o for o, lay in self._pending.items() if lay is layer]:
                del self._pending[origin]
        self._persist()

    async def clear_pending(self, context, layer: BrowserLayer) -> None:
        """At launch: clear the storage a previous launch, or a previous MCP
        process (the pending file), left pending."""
        if layer is BrowserLayer.CAMOUFOX:
            self._load()
        origins = {o for o, lay in self._pending.items() if lay is layer}
        if origins:
            await self._clear(context, origins, layer)

    async def finish_all(self) -> None:
        """MCP exit: finish every armed tab while its browser is still up, so
        its storage clear runs before the drain and the browser shutdown. A
        tab that will not finish in time stays armed; the layer cleanup then
        leaves its origins pending, and they are in the pending file."""
        for page, entry in list(self._tabs.items()):
            if entry.state is State.CLOSING:
                continue  # a finish is running; its clear is drained
            try:
                await asyncio.wait_for(self.finish(page), timeout=_CLEAR_TIMEOUT_MS / 1000 * 3)
            except Exception as e:
                logger.warning("payment tab not finished at exit: %s", type(e).__name__)

    # --- the pending file --------------------------------------------------
    def _on_disk(self) -> frozenset[str]:
        """Camoufox origins whose storage may hold what a payment tab showed:
        pending clears, and every armed tab's."""
        cam = BrowserLayer.CAMOUFOX
        armed = (o for e in self._entries() if e.layer is cam for o in e.origins)
        return frozenset(o for o, lay in self._pending.items() if lay is cam) | frozenset(armed)

    def _persist(self) -> bool:
        """Bring this process's entries in the pending file up to date, under
        a lock: another MCP process's entries are kept (each CC session runs
        its own, and they share the Camoufox profile). Atomic and 0600; an
        empty file is removed. False when it could not be written, or the file
        is unreadable (kept for the owner to inspect)."""
        want = self._on_disk()
        if self._store_broken:
            return False
        if want == self._written:
            return True
        path = env.payment_tab_pending_path()
        try:
            with _locked(path):
                disk = _read_pending(path)
                if disk is None:
                    self._mark_broken(path)
                    return False
                _write_pending(path, (disk - (self._written - want)) | want)
        except OSError as e:
            logger.warning("payment tab pending file not written: %s", type(e).__name__)
            return False
        self._written = want
        return True

    def _load(self) -> None:
        """At a Camoufox launch: take in the pending file other or earlier MCP
        processes left, and remove temp files an interrupted write left. One
        that cannot be read keeps run_js and arming refused (its origins are
        unknown, so their storage cannot be cleared) until a launch finds it
        readable or removed."""
        path = env.payment_tab_pending_path()
        try:
            with _locked(path):
                for stray in path.parent.glob(f".{path.name}.*"):
                    with contextlib.suppress(OSError):
                        stray.unlink()
                disk = _read_pending(path)
        except OSError:
            disk = None
        if disk is None:
            self._mark_broken(path)
            return
        self._store_broken = False
        for origin in disk:
            self._pending.setdefault(origin, BrowserLayer.CAMOUFOX)
        self._written |= disk  # now this process's to clear
        self._persist()

    def _mark_broken(self, path) -> None:
        self._store_broken = True
        logger.warning(
            "payment tab pending file %s unreadable: run_js and card fill stay refused", path
        )

    async def drain(self) -> None:
        """Wait for storage clears and mask tasks still running (MCP exit)."""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=_CLEAR_TIMEOUT_MS / 1000 * 3)

    # --- redaction -------------------------------------------------------
    @staticmethod
    def _mac(key: bytes, text: str) -> bytes:
        return hmac.new(key, text.encode("utf-8", "surrogatepass"), hashlib.sha256).digest()

    def _matches(self, token: str) -> bool:
        """The token, its digits, its letters and digits run together, and each
        letter-or-digit run of its URL-decoded form (so ``Tester.``,
        ``Dec-2034`` and ``?cvc=739&n=Pat+Tester`` match too), all folded as
        :meth:`_register` stores them."""
        token = _nfkc(token)
        decoded = _nfkc(unquote_plus(token))
        runs = _ALNUM_RUN.findall(decoded)
        cands = {token, "".join(c for c in token if c.isdigit()), "".join(runs), *runs}
        return any(self._known(_fold(c)) for c in cands)

    def _entries(self) -> list[_Arming]:
        """Every live arming: those of armed tabs and of their popups."""
        return list({id(e): e for e in (*self._tabs.values(), *self._popups.values())}.values())

    def _known(self, cand: str) -> bool:
        """``cand`` (already folded) is a registered part of a filled value."""
        for entry in self._entries():
            if len(cand) in entry.lengths and self._mac(entry.key, cand) in entry.digests:
                return True
        return False

    def _hide_spans(self, text: str) -> str:
        """A value the page reformatted with separators (``12 / 34``,
        ``7 3 9``, ``SW1A 1AA``) splits into parts too short to match alone.
        Every run of consecutive letter-or-digit parts joined by
        :data:`_SEP` is compared as their concatenation, against the
        registered digits-only and run-together forms; a match hides the
        whole stretch from its first part to its last."""
        longest = max((max(e.lengths) for e in self._entries() if e.lengths), default=0)
        cut: list[tuple[int, int]] = []
        for span in _SPAN.finditer(text):
            parts = [
                (span.start() + r.start(), span.start() + r.end(), _fold(r.group()))
                for r in _ALNUM_RUN.finditer(span.group())
            ]
            for i in range(len(parts)):
                joined = parts[i][2]
                for j in range(i + 1, len(parts)):
                    joined += parts[j][2]
                    if len(joined) > longest:
                        break
                    if self._known(joined):
                        cut.append((parts[i][0], parts[j][1]))
        if not cut:
            return text
        out, pos = [], 0
        for start, end in sorted(cut):
            if end <= pos:
                continue
            if start >= pos:
                out.append(text[pos:start])
                out.append(HIDDEN)
            pos = end
        out.append(text[pos:])
        return "".join(out)

    @staticmethod
    def _redact_cards(text: str) -> str:
        """Card numbers in ``text`` (card_redaction.redact_text: split across
        separators and JSON escapes included). It runs BEFORE any value is
        swapped for :data:`HIDDEN`: the longer marker can widen the gaps
        between a split card's digit groups past the split-card limit. A text
        it cannot walk (a JSON text too deep) is hidden whole."""
        try:
            return redact_cards(text)
        except Exception:
            return HIDDEN

    def _hide_tokens(self, text: str) -> str:
        if any(e.digests for e in self._entries()):
            text = _TEXT_TOKEN.sub(
                lambda m: HIDDEN if self._matches(m.group(0)) else m.group(0), text
            )
            text = self._hide_spans(text)
        return text

    def redact_text(self, text):
        """Card numbers, then tokens whose HMAC matches a filled value.
        Anything but a string is returned unchanged."""
        if not isinstance(text, str):
            return text
        return self._hide_tokens(self._redact_cards(text))

    def redact_snapshot(self, page, text, armed_before: bool, epoch_before: int):
        """A snapshot of ``page``, read while ``armed_before`` held and the
        epoch was ``epoch_before``: card numbers, then (armed) form values,
        then filled-value tokens. Arming is checked again now: a page armed
        during the read counts as armed."""
        if not isinstance(text, str):
            return text
        text = self._redact_cards(text)
        if armed_before or self.is_armed(page) or self.epoch != epoch_before:
            text = hide_form_values(text)
        return self._hide_tokens(text)

    def path_only(self, url):
        """``url`` without query, fragment or userinfo, redacted."""
        if not isinstance(url, str):
            return url
        try:
            parts = urlsplit(url)
            host, port = parts.hostname, parts.port
        except ValueError:
            return HIDDEN
        if not parts.scheme or not host:
            return self.redact_text(parts.scheme + ":" if parts.scheme else HIDDEN)
        host = f"[{host}]" if ":" in host else host
        return self.redact_text(
            f"{parts.scheme}://{host}" + (f":{port}" if port else "") + parts.path
        )

    # --- mask ------------------------------------------------------------
    async def apply_mask(self, page) -> None:
        for frame in list(page.frames):
            with contextlib.suppress(Exception):
                await frame.evaluate(_MASK_JS)

    async def verify_mask(self, page) -> bool:
        """True when every form control in every frame computes as masked.
        Recorded on the arming: :meth:`begin_fill` needs the last one True."""
        entry = self._tabs.get(page)
        gen = entry.doc_gen if entry is not None else 0
        ok = await self._mask_ok(page)
        if entry is not None and self._tabs.get(page) is entry:
            # A document that arrived during the check was not the one checked.
            ok = ok and entry.doc_gen == gen
            entry.masked = ok
        return ok

    async def _mask_ok(self, page) -> bool:
        try:
            frames = list(page.frames)
            for frame in frames:
                if await frame.evaluate(_MASK_CHECK_JS) is not True:
                    return False
            return bool(frames)
        except Exception as e:
            logger.warning("payment tab mask check failed: %s", type(e).__name__)
            return False

    # --- events ----------------------------------------------------------
    def _spawn(self, coro) -> asyncio.Task | None:
        try:
            task = asyncio.ensure_future(coro)
        except RuntimeError:  # no running loop
            coro.close()
            return None
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def _remask(self, page, frame) -> None:
        entry = self._entry_of(page)
        if entry is None:
            return
        if frame is not None and page is entry.page:
            # A new document: begin_fill needs a new mask check.
            entry.masked = False
            entry.doc_gen += 1
        # Every origin the tab (or its popup) shows has its storage cleared at
        # finish, not only those present at arm, fill and finish.
        with contextlib.suppress(Exception):
            origin = origin_of(frame.url) if frame is not None else None
            if origin and origin not in entry.origins:
                entry.origins.add(origin)
                self._persist_or_close(entry)
        if frame is None:
            self._spawn(self.apply_mask(page))
        else:
            self._spawn(_mask_frame(frame))

    def _adopt(self, opener, popup) -> None:
        """``opener`` (an armed tab or its popup) opened ``popup``: it is
        guarded as the tab is (:meth:`is_armed`), masked, and its origins are
        cleared with the tab's."""
        entry = self._entry_of(opener)
        if entry is None or popup in self._popups:
            return
        self._popups[popup] = entry
        entry.origins |= _frame_origins(popup)
        if popup not in self._listening:
            self._listening.add(popup)
            popup.once("close", lambda _p: self._on_popup_close(popup))
            popup.on("framenavigated", lambda frame: self._remask(popup, frame))
            popup.on("load", lambda _p: self._remask(popup, None))
            popup.on("popup", lambda child: self._adopt(popup, child))
        self._persist_or_close(entry)
        self._spawn(self._mask_popup(popup))

    async def _mask_popup(self, popup) -> None:
        try:
            await popup.add_init_script(_MASK_INIT_JS)
        except Exception as e:
            # Unmasked for its later documents: it is closed instead.
            logger.warning("payment tab popup mask failed: %s", type(e).__name__)
            with contextlib.suppress(Exception):
                await popup.close(run_before_unload=False)
            return
        await self.apply_mask(popup)

    def _on_popup_close(self, popup) -> None:
        entry = self._popups.pop(popup, None)
        if entry is None or entry.page in self._tabs:
            return  # the tab's own finish clears what both showed
        # The tab is gone already (its clear ran): clear what this one showed too.
        origins = set(entry.origins)
        for origin in origins:
            self._pending[origin] = entry.layer
        context = None
        with contextlib.suppress(Exception):
            context = popup.context
        self._spawn(self._clear(context, origins, entry.layer))

    def _on_crash(self, page) -> None:
        entry = self._tabs.get(page)
        if entry is not None:
            entry.crashed = True
            logger.warning("payment tab crashed: it stays armed until it closes")

    def _on_close(self, page) -> None:
        entry = self._tabs.pop(page, None)
        if entry is None:
            return
        self.finished += 1
        for popup in self._popups_of(entry):  # closed by the page or the VNC user
            self._spawn(_close_quietly(popup))
        origins = set(entry.origins)
        for origin in origins:
            self._pending[origin] = entry.layer
        context = None
        with contextlib.suppress(Exception):
            context = page.context
        task = self._spawn(self._clear(context, origins, entry.layer))
        if task is not None:
            self._clears[page] = task  # weak: finish pops it, a closed page drops it

    async def _clear(self, context, origins: set[str], layer: BrowserLayer) -> bool:
        ok = bool(origins) and context is not None and await clear_site_storage(context, origins)
        if ok or not origins:
            for origin in origins:
                if self._pending.get(origin) is layer:
                    del self._pending[origin]
            self._persist()  # an entry leaves the file only after its clear
        else:
            logger.warning(
                "payment tab site storage not cleared: run_js stays refused until the browser restarts"
            )
        return ok or not origins

    def _require(self, page) -> _Arming:
        entry = self._tabs.get(page)
        if entry is None:
            raise PaymentTabError("no_payment_tab")
        return entry


async def _close_quietly(page) -> None:
    with contextlib.suppress(Exception):
        await page.close(run_before_unload=False)


async def _mask_frame(frame) -> None:
    with contextlib.suppress(Exception):  # a frame detached meanwhile
        await frame.evaluate(_MASK_JS)


@contextlib.contextmanager
def _locked(path):
    """An exclusive lock on the pending file's sidecar, held for one read-merge-write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path.parent / f"{path.name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # releases the lock


def _read_pending(path) -> frozenset[str] | None:
    """The origins in the pending file; empty when there is none, None when
    its content is not a JSON list of origins. Other OSErrors propagate."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return frozenset()
    except ValueError:
        return None
    if isinstance(data, list) and all(isinstance(o, str) and origin_of(o) == o for o in data):
        return frozenset(data)
    return None


def _write_pending(path, origins: frozenset[str]) -> None:
    if not origins:
        path.unlink(missing_ok=True)
        return
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")  # 0600
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(sorted(origins), f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def is_closed(page) -> bool:
    try:
        return bool(page.is_closed())
    except Exception:  # a disposed page
        return True


async def clear_site_storage(context, origins: set[str]) -> bool:
    """Clear the site storage of ``origins`` in ``context`` without touching
    the network: a scratch page whose requests are all answered locally loads
    a blank document at each origin, with the other origins as iframes (their
    storage there is partitioned under it), and clears every frame."""
    page = None
    ordered = sorted(origins)

    async def answer(route) -> None:
        url = route.request.url
        if urlsplit(url).path != _CLEAR_PATH:
            await route.abort()
            return
        frames = ""
        if url.endswith("?top"):
            me = origin_of(url)
            frames = "".join(
                f'<iframe src="{o}{_CLEAR_PATH}"></iframe>' for o in ordered if o != me
            )
        await route.fulfill(
            status=200, content_type="text/html", body=f"<!doctype html><title>.</title>{frames}"
        )

    try:
        page = await context.new_page()
        await page.route("**/*", answer)
        ok = True
        for origin in ordered:
            await page.goto(
                f"{origin}{_CLEAR_PATH}?top", wait_until="load", timeout=_CLEAR_TIMEOUT_MS
            )
            for frame in page.frames:
                ok = await frame.evaluate(_CLEAR_STORAGE_JS) is True and ok
        return ok
    except Exception as e:
        logger.warning("payment tab storage clear failed: %s", type(e).__name__)
        return False
    finally:
        if page is not None:
            with contextlib.suppress(Exception):
                # A closed tab can be reopened from X input (Ctrl+Shift+T works
                # even with max_tabs_undo=0, MEASURED); left on about:blank, a
                # reopened scratch page sends the site no request.
                await page.goto("about:blank")
            with contextlib.suppress(Exception):
                await page.close(run_before_unload=False)


TABS = PaymentTabs()
CARD_FILTER.armed = TABS.any_armed
CARD_FILTER.redact_armed = TABS.redact_text  # an f-string message can carry a filled value


# The arming API the card-fill flow calls (and the tests).
async def arm(page, layer: BrowserLayer = BrowserLayer.CAMOUFOX) -> bool:
    return await TABS.arm(page, layer)


def begin_fill(page, values_for_hmac) -> None:
    TABS.begin_fill(page, values_for_hmac)


def mark_filled(page, values_for_hmac=()) -> None:
    TABS.mark_filled(page, values_for_hmac)


def disarm(page) -> None:
    TABS.disarm(page)


async def finish(page) -> dict:
    return await TABS.finish(page)
