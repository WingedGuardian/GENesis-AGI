"""The payment-tab guard: while a tab holds a filled card, no tool reads it.

A Camoufox tab the card-fill flow (#3172) fills is *armed* from the moment it
is chosen until it closes. Arming is keyed by the Playwright ``Page``, never by
host or timer, and the browser tools (``browser.py``) consult it:

* run_js is refused in the whole browser while any tab is armed, or while a
  closed payment tab's site storage is still being cleared;
* the armed tab refuses screenshot, fill, upload and navigate, and press_key
  takes only the keys in :data:`ALLOWED_KEYS`;
* every page snapshot goes through :meth:`PaymentTabs.redact_snapshot`;
* ``_ts_log`` writes nothing and ``CARD_FILTER`` withholds log arguments.

Back restores filled values from session history in every pref configuration
(measured), so the flow ends by CLOSING the tab (:meth:`PaymentTabs.finish`),
which also clears the site storage of the origins the tab showed.

The *taint*: any run_js, or any non-http(s) navigation, since this Camoufox
launch taints the browser context, and arming then refuses with
``browser_tainted_restart``. A script planted in another tab can reach an armed
tab through ``window.opener`` or the handle ``window.open`` returned, so a
per-page check is not enough. Only a Camoufox restart clears it.

Filled values are never kept. :meth:`PaymentTabs.mark_filled` stores HMACs of
them under a key made for that arming, and snapshot tokens whose HMAC matches
are hidden.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import hashlib
import hmac
import logging
import os
import re
import secrets
import weakref
from dataclasses import dataclass, field
from urllib.parse import unquote_plus, urlsplit

import yaml

from genesis.browser.types import BrowserLayer
from genesis.security.card_redaction import CARD_FILTER
from genesis.security.card_shapes import redact

logger = logging.getLogger(__name__)

HIDDEN = "[hidden: payment tab]"

# Pinned at launch while card fill is enabled. The first six keep form data
# out of form history, autofill and the session store; max_tabs_undo=0 makes a
# closed payment tab unrestorable, and clipboard.autocopy=false stops X11
# primary-selection copies of selected text.
CARD_FILL_PREFS = {
    "browser.formfill.enable": False,
    "signon.autofillForms": False,
    "extensions.formautofill.creditCards.enabled": False,
    "extensions.formautofill.addresses.enabled": False,
    "browser.sessionstore.privacy_level": 2,
    "browser.sessionhistory.max_total_viewers": 0,
    "browser.sessionstore.max_tabs_undo": 0,
    "clipboard.autocopy": False,
}

# Keys browser_press_key sends to an armed tab: focus movement, activation and
# scrolling. Nothing that selects, copies, pastes, deletes or edits.
ALLOWED_KEYS = frozenset(
    {
        "Enter",
        "Tab",
        "Shift+Tab",
        "Escape",
        "Space",
        " ",
        "ArrowUp",
        "ArrowDown",
        "ArrowLeft",
        "ArrowRight",
        "PageUp",
        "PageDown",
        "Home",
        "End",
    }
)


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
    ``browser_tainted_restart``, ``already_armed``, ``no_payment_tab``,
    ``wrong_state``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# The mask: form controls of an armed tab draw no glyphs, caret or selection,
# so neither a screenshot nor the VNC view shows a value. Applied in every
# frame, again on each new document (Back builds one), and checked by
# _MASK_CHECK_JS.
MASK_CSS = (
    "input, textarea, select, [contenteditable] {"
    " color: transparent !important; -webkit-text-fill-color: transparent !important;"
    " caret-color: transparent !important; text-shadow: none !important }"
    " ::selection { color: transparent !important; background: transparent !important }"
)
_MASK_JS = (
    "() => { const id = '__genesis_payment_mask__';"
    " if (!document.documentElement) return false;"
    " let s = document.getElementById(id);"
    " if (!s) { s = document.createElement('style'); s.id = id;"
    " (document.head || document.documentElement).appendChild(s); }"
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
  for (const e of document.querySelectorAll('input, textarea, select')) {
    const s = getComputedStyle(e);
    if (!clear(s.color) || !clear(s.webkitTextFillColor) || !clear(s.caretColor)) return false;
    if (s.textShadow && s.textShadow !== 'none') return false;
  }
  return true;
}"""
# Clears the frame's origin storage (local, session, IndexedDB, Cache API).
# Cookies are left: they hold the site login, and no browser stores form
# values in them.
_CLEAR_STORAGE_JS = """async () => {
  let ok = true;
  try { localStorage.clear(); } catch (e) { ok = false; }
  try { sessionStorage.clear(); } catch (e) { ok = false; }
  try {
    if (indexedDB.databases) {
      for (const d of await indexedDB.databases()) {
        await new Promise((r) => {
          const q = indexedDB.deleteDatabase(d.name);
          q.onsuccess = q.onerror = q.onblocked = () => r();
        });
      }
    }
  } catch (e) { ok = false; }
  try {
    if (self.caches) for (const k of await caches.keys()) await caches.delete(k);
  } catch (e) { ok = false; }
  return ok;
}"""
_CLEAR_PATH = "/__genesis_payment_tab_clear__"
_CLEAR_TIMEOUT_MS = 10000  # a fulfilled, network-free document loads in milliseconds

# One aria-snapshot line of a value-bearing control: indent, role, then the
# quoted name and [attributes], then ": value" (or ":" before children).
_CONTROL_ROLES = ("textbox", "searchbox", "spinbutton", "combobox")
_CONTROL_LINE = re.compile(
    r"^(?P<head>(?P<indent>\s*)- (?:" + "|".join(_CONTROL_ROLES) + r")"
    r'(?: "(?:[^"\\]|\\.)*")?(?: \[[^\]\n]*\])*)(?P<rest>.*)$'
)
_CONTROL_START = re.compile(r"^\s*- (?:" + "|".join(_CONTROL_ROLES) + r")\b")
_OPTION_START = re.compile(r"^\s*- option\b")
_TEXT_TOKEN = re.compile(r"[^\s\"'`<>()\[\]{}:;,|]+")
_ALNUM_RUN = re.compile(r"[0-9A-Za-z]+")
_MIN_PART = 3  # shorter value parts are not matched: too common to redact on sight


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def hide_form_values(text: str) -> str:
    """Steps 1 and 2 of the armed-snapshot redaction: every textbox, searchbox,
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
                role = line.strip()[2:].split(" ", 1)[0].split(":", 1)[0]
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


class PaymentTabs:
    """The arming state of one MCP process. :data:`TABS` is the instance."""

    def __init__(self) -> None:
        # Set by browser.py: moves the tools off a page that is about to close.
        self.move_off = None
        self.reset()

    def reset(self) -> None:
        """Forget everything (tests, and the MCP exit)."""
        self._tabs: dict = {}
        self._pending: dict[str, BrowserLayer] = {}  # origin -> layer, storage not yet cleared
        self._clears = weakref.WeakKeyDictionary()  # closed page -> its storage-clear task
        self._tasks: set[asyncio.Task] = set()
        self._listening = weakref.WeakSet()  # pages whose event handlers are registered
        self._tainted: str | None = None
        self.prefs_pinned = False
        self.epoch = 0  # bumped by every arm, so a read can tell one began meanwhile

    # --- reads -----------------------------------------------------------
    def any_armed(self) -> bool:
        return bool(self._tabs)

    def is_armed(self, page) -> bool:
        return page is not None and page in self._tabs

    def crashed(self, page) -> bool:
        entry = self._tabs.get(page) if page is not None else None
        return entry is not None and entry.crashed

    def state(self, page) -> State | None:
        entry = self._tabs.get(page) if page is not None else None
        return None if entry is None else entry.state

    def run_js_blocked(self) -> bool:
        """True while a tab is armed, or a closed one's storage is not yet cleared."""
        return bool(self._tabs or self._pending)

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

    # --- arming ----------------------------------------------------------
    async def arm(self, page, layer: BrowserLayer = BrowserLayer.CAMOUFOX) -> bool:
        """Arm ``page`` (AWAITING_TAP) and mask it. Returns whether the mask
        verified in every frame. Raises :class:`PaymentTabError`."""
        if page in self._tabs:
            raise PaymentTabError("already_armed")
        if self._tabs:
            raise PaymentTabError("other_payment_tab")
        if self._tainted is not None:
            raise PaymentTabError("browser_tainted_restart")
        # Registered before the first await: every guard holds from here on.
        entry = self._tabs[page] = _Arming(layer=layer)
        self.epoch += 1
        entry.origins |= _frame_origins(page)
        if page not in self._listening:  # each handler does nothing once disarmed
            self._listening.add(page)
            page.once("close", lambda _p: self._on_close(page))
            page.on("crash", lambda _p: self._on_crash(page))
            page.on("framenavigated", lambda frame: self._remask(page, frame))
            page.on("load", lambda _p: self._remask(page, None))
        try:
            await page.add_init_script(_MASK_INIT_JS)
        except Exception as e:
            logger.warning("payment tab mask init script failed: %s", type(e).__name__)
        await self.apply_mask(page)
        return await self.verify_mask(page)

    def begin_fill(self, page) -> None:
        entry = self._require(page)
        if entry.state is not State.AWAITING_TAP:
            raise PaymentTabError("wrong_state")
        entry.state = State.FILLING

    def mark_filled(self, page, values_for_hmac) -> None:
        """Record HMACs of the filled values (never the values) and move to
        ARMED. Each value is matched whole, as digits only, and by its
        whitespace-separated parts of at least three characters."""
        entry = self._require(page)
        if entry.state not in (State.AWAITING_TAP, State.FILLING, State.ARMED):
            raise PaymentTabError("wrong_state")
        for value in values_for_hmac:
            v = str(value).strip()
            digits = "".join(c for c in v if c.isdigit())
            for part in {v, digits, *v.split()}:
                if len(part) >= _MIN_PART:
                    entry.digests.add(self._mac(entry.key, part))
                    entry.lengths.add(len(part))
        entry.origins |= _frame_origins(page)
        entry.state = State.ARMED

    def disarm(self, page) -> None:
        """Release a tab nothing was filled into (AWAITING_TAP). A filled tab
        must be closed with :meth:`finish`."""
        entry = self._require(page)
        if entry.state is not State.AWAITING_TAP:
            raise PaymentTabError("wrong_state")
        # Its listeners do nothing once it is not in _tabs. The mask init script
        # cannot be removed (Playwright has no API for it), so the tab's later
        # documents stay masked on screen.
        del self._tabs[page]

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
        entry.state = State.CLOSING
        entry.origins |= _frame_origins(page)
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
        for page in [p for p, e in self._tabs.items() if e.layer is layer]:
            entry = self._tabs.pop(page)
            if layer is BrowserLayer.CAMOUFOX:
                for origin in entry.origins:
                    self._pending[origin] = layer
        if layer is not BrowserLayer.CAMOUFOX:
            for origin in [o for o, lay in self._pending.items() if lay is layer]:
                del self._pending[origin]

    async def clear_pending(self, context, layer: BrowserLayer) -> None:
        """At launch: clear the storage a previous launch left pending."""
        origins = {o for o, lay in self._pending.items() if lay is layer}
        if origins:
            await self._clear(context, origins, layer)

    async def drain(self) -> None:
        """Wait for storage clears and mask tasks still running (MCP exit)."""
        if self._tasks:
            await asyncio.wait(set(self._tasks), timeout=_CLEAR_TIMEOUT_MS / 1000 * 3)

    # --- redaction -------------------------------------------------------
    @staticmethod
    def _mac(key: bytes, text: str) -> bytes:
        return hmac.new(key, text.encode("utf-8", "surrogatepass"), hashlib.sha256).digest()

    def _matches(self, token: str) -> bool:
        """The token, its digits, and each letter-or-digit run of its URL-decoded
        form (so ``Tester.`` and ``?cvc=739&n=Pat+Tester`` match too)."""
        decoded = unquote_plus(token)
        cands = {token, "".join(c for c in token if c.isdigit()), *_ALNUM_RUN.findall(decoded)}
        for entry in self._tabs.values():
            for cand in cands:
                if len(cand) in entry.lengths and self._mac(entry.key, cand) in entry.digests:
                    return True
        return False

    def redact_text(self, text):
        """Step 3 then 4: tokens whose HMAC matches a filled value, then card
        numbers (card_shapes). Anything but a string is returned unchanged."""
        if not isinstance(text, str):
            return text
        if any(e.digests for e in self._tabs.values()):
            text = _TEXT_TOKEN.sub(
                lambda m: HIDDEN if self._matches(m.group(0)) else m.group(0), text
            )
        return redact(text)

    def redact_snapshot(self, page, text, armed_before: bool, epoch_before: int):
        """A snapshot of ``page``, read while ``armed_before`` held and the
        epoch was ``epoch_before``. Arming is checked again now: a page armed
        during the read counts as armed."""
        if not isinstance(text, str):
            return text
        if armed_before or self.is_armed(page) or self.epoch != epoch_before:
            text = hide_form_values(text)
        return self.redact_text(text)

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
        """True when every form control in every frame computes as masked."""
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
        entry = self._tabs.get(page)
        if entry is None:
            return
        # Every origin the tab shows has its storage cleared at finish, not
        # only those present at arm, fill and finish (a redirect in between).
        with contextlib.suppress(Exception):
            if frame is not None and (origin := origin_of(frame.url)):
                entry.origins.add(origin)
        if frame is None:
            self._spawn(self.apply_mask(page))
        else:
            self._spawn(_mask_frame(frame))

    def _on_crash(self, page) -> None:
        entry = self._tabs.get(page)
        if entry is not None:
            entry.crashed = True
            logger.warning("payment tab crashed: it stays armed until it closes")

    def _on_close(self, page) -> None:
        entry = self._tabs.pop(page, None)
        if entry is None:
            return
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


async def _mask_frame(frame) -> None:
    with contextlib.suppress(Exception):  # a frame detached meanwhile
        await frame.evaluate(_MASK_JS)


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


# The arming API the card-fill flow calls (and the tests).
async def arm(page, layer: BrowserLayer = BrowserLayer.CAMOUFOX) -> bool:
    return await TABS.arm(page, layer)


def begin_fill(page) -> None:
    TABS.begin_fill(page)


def mark_filled(page, values_for_hmac) -> None:
    TABS.mark_filled(page, values_for_hmac)


def disarm(page) -> None:
    TABS.disarm(page)


async def finish(page) -> dict:
    return await TABS.finish(page)
