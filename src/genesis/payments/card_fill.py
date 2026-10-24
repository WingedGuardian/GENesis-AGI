"""browser_fill_card's rules: no browser objects here.

The switch (``card_fill_enabled``) and the launch prefs, the argument checks,
the values typed for each field kind, the amount check, the handover filter,
and the fixed result texts. The flow that uses them is
``genesis.mcp.health.payment_context``.

Every result is a fixed text under a fixed code. Nothing here returns, logs or
quotes a card value, a page string or an exception's text.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from urllib.parse import urlsplit

import yaml

from genesis.browser.profile import domain_matches
from genesis.payments.approval import holds_long_digit_run
from genesis.payments.release import FILL_NAMES
from genesis.payments.vault_store import CARD_REF, VaultError, normalise_merchant
from genesis.security.card_shapes import contains_card_number

logger = logging.getLogger(__name__)

# Pinned at Camoufox launch while card fill is on, for all browsing (owner
# ruling): no form history, autofill, password capture (a CVC field can be
# type=password), restorable closed tabs, session-store form data, cached
# documents, X11 primary-selection copies, places history or disk cache; and
# no accessibility tree (owner ruling Q14): an AT-SPI client of the same user
# read a masked page's typed card number off it (measured), and with this pref
# Camoufox does not expose the tree.
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
    "accessibility.force_disabled": 1,
}

CARD_KINDS = ("number", "expiry", "exp_month", "exp_year", "cvc", "name", "postal_code")
EXPIRY_FORMATS = ("MM/YY", "MM / YY", "MM-YY", "MMYY", "MM/YYYY", "MMYYYY")
MAX_FRAMES = 4
MAX_SELECTOR = 500
MAX_EXTRA_VALUE = 256
# A vault refusal reason is one of release.py's fixed texts (lowercase words,
# no digits); anything else is not passed on.
_VAULT_REASON = re.compile(r"[a-z][a-z ,'()-]{0,99}")
_NONE = " Nothing was filled."
_CANCELLED = " The request was cancelled. Nothing was filled."
_SPENT = " A single-use card is now spent."
_NO_RETRY = " Outcome unknown: do NOT retry; the owner checks the merchant account or statement."

TEXTS = {
    "card_fill_disabled": "Card fill is off (config/payments.yaml card_fill.enabled, or "
    "GENESIS_CARD_FILL_DISABLED=1). Hand the payment to the owner.",
    "playwright_debug_on": "Refused: Playwright or Firefox debug logging is on; it could log "
    "what is typed.",
    "bad_card_ref": "card_ref must be card_ followed by 12 hex characters.",
    "bad_amount": "amount must be the total as the page states it (1-32 printable characters, "
    "with its digits).",
    "bad_fields": "fields must map number (required), expiry or exp_month+exp_year, cvc, "
    "name, postal_code to a selector or {selector, frames}.",
    "bad_pay": "pay must be the Pay control's selector, or {selector, frames}.",
    "bad_extra_fields": "extra_fields must map names from (" + ", ".join(FILL_NAMES) + ") "
    "to {selector, value}; no long digit run, no name already in fields.",
    "bad_expiry_format": "expiry_format must be one of: " + ", ".join(EXPIRY_FORMATS) + ".",
    "no_page": "No page open. Open the checkout with browser_navigate first.",
    "not_camoufox": "Card fill works only on Camoufox: browser_navigate(stealth=True).",
    "not_http": "The checkout page is not an http(s) page.",
    "checkout_on_public_suffix": "The checkout's host is itself a public suffix, a domain "
    "shared by many unrelated sites; refused.",
    "prefs_not_pinned": "Camoufox started before card fill was on. Restart the browser first.",
    "browser_packages_changed": "The browser packages changed; restart the session first.",
    "payment_in_flight": "A card payment is in progress; wait for it to finish.",
    "turnstile_in_progress": "A Turnstile check is clicking on the display; retry shortly.",
    "frame_not_found": "A frame selector did not match exactly one iframe.",
    "checkout_load_failed": "The checkout did not load in the payment window." + _NONE,
    "checkout_redirected": "The checkout redirected to another site." + _NONE,
    "bot_check": "The checkout showed a bot check. Hand the payment to the owner." + _NONE,
    "field_not_found": "A card field selector matched nothing.",
    "field_ambiguous": "A card field selector matched more than one element; narrow it.",
    "field_mismatch": "A target is not that kind of card field (autocomplete or label).",
    "field_origin": "A card field's or Pay's frame is not on https at a named host.",
    "not_https": "The checkout is not served over https at a named host." + _NONE,
    "pay_not_found": "The pay selector did not match exactly one element.",
    "pay_not_button": "The pay control must be a button or an input type=submit.",
    "pay_get_form": "Refused: the pay control's form declares GET, or would send card fields "
    "without the number in a URL.",
    "extra_field_not_found": "An extra field selector did not match one input or select of "
    "its own.",
    "extra_field_is_card_field": "An extra_fields target is a card field.",
    "extra_field_failed": "An extra field did not take or keep its value (pass it as the "
    "page formats it)." + _NONE,
    "mask_unverified": "The payment window's screen mask did not verify." + _NONE,
    "amount_not_on_page": "The stated amount is not on the checkout page." + _NONE,
    "vault_unavailable": "The vault service did not answer (genesis-server)." + _NONE,
    "bad_reply": "The vault service's answer was not understood." + _NONE,
    "approval_text_refused": "The tap cannot show the title, amount or button (12+ digits "
    "in a run). Hand the payment to the owner." + _NONE,
    "release_refused": "The vault refused the release (reason given)." + _NONE,
    "not_approved": "The owner did not approve." + _NONE,
    "release_expired": "The owner did not approve in time." + _NONE,
    "release_timed_out": "No decision arrived in time." + _CANCELLED,
    "checkout_closed": "The payment window closed before the hand-over." + _CANCELLED,
    "browser_closed": "The browser closed or restarted before the hand-over." + _CANCELLED,
    "page_changed": "The checkout changed while waiting for approval." + _CANCELLED,
    "claim_refused": "The vault refused to hand over the card (reason given)." + _NONE,
    "claim_unanswered": "The vault did not answer the hand-over; a single-use card may now "
    "be spent." + _NONE,
    "claimed_not_filled": "The checkout changed after the hand-over; nothing was typed and "
    "Pay was not clicked." + _SPENT,
    "failed_before_pay": "Filling stopped part-way; Pay was NOT clicked and the payment "
    "window was closed." + _SPENT,
    "card_url_before_pay": "Pay was NOT clicked: while the card was typed, the checkout sent "
    "or tried to send the card number in a URL (and may have moved on with it). Treat the "
    "card as exposed and tell the owner." + _NO_RETRY,
    "submitted_unverified": "Pay was clicked and the checkout moved on. Not verified: "
    "check the order page or the merchant account.",
    "challenge": "Pay was clicked, then a bank check appeared or the checkout did not move "
    "on within 60 s; the payment window was closed." + _NO_RETRY,
    "pay_click_unknown": "The Pay click raised an error and the checkout did not move on: "
    "it is unknown whether Pay was clicked; the payment window was closed." + _NO_RETRY,
    "moved_while_filling": "Pay was NOT clicked, but the checkout moved on (navigated or "
    "opened a window) while the card was typed: it may have submitted the payment itself. "
    "The payment window was closed." + _NO_RETRY,
    "card_url_blocked": "Pay was clicked; the payment window blocked a request that would have "
    "carried the card number in a URL. Whether the checkout went ahead another way is "
    "unknown." + _NO_RETRY,
    "card_url_leaked": "Pay was clicked; a redirect carried (or may have carried) the card number "
    "in a URL, which the payment window could see but not stop." + _NO_RETRY,
    "outcome_unknown": "Pay was clicked, then the payment window, the browser or this call "
    "ended before the checkout settled." + _NO_RETRY,
    "internal_error": "browser_fill_card failed before Pay; detail withheld.",
}


class Refused(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


Spec = tuple[tuple[str, ...], str]  # (iframe selectors, outermost first; selector)


@dataclass(frozen=True)
class CardArgs:
    card_ref: str
    amount: str
    fields: dict[str, Spec]  # kind -> spec, in CARD_KINDS order
    pay: Spec
    expiry_format: str
    extra: dict[str, tuple[str, str]]  # name -> (selector, value), in FILL_NAMES order

    def __repr__(self) -> str:  # never the extra values
        return f"CardArgs({sorted(self.fields)}, extra={list(self.extra)})"


def result(code: str, host: str = "", **extra) -> dict:
    base = code.split(":", 1)[0]
    out = {"code": code, "status": TEXTS.get(base, TEXTS["internal_error"]), "host": host}
    reason = extra.get("reason")
    if isinstance(reason, str) and _VAULT_REASON.fullmatch(reason):
        out["reason"] = reason
    if extra.get("field") in CARD_KINDS:
        out["field"] = extra["field"]
    if extra.get("filled") is not None:
        out["filled"] = [k for k in extra["filled"] if k in CARD_KINDS]
    return out


def card_fill_enabled() -> bool:
    """``card_fill.enabled`` in ``config/payments.yaml`` plus its ``.local.yaml``
    overlay. Off unless it is exactly ``true``; a missing or unreadable file
    means off, and ``GENESIS_CARD_FILL_DISABLED=1`` turns it off whatever the
    files say. Read on every call."""
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


# Firefox's log configuration variables, the older names included: it reads
# MOZ_LOG, then MOZ_LOG_MODULES, then NSPR_LOG_MODULES, and MOZ_LOG_FILE, then
# NSPR_LOG_FILE (xpcom/base/Logging.cpp).
_FIREFOX_LOG_VARS = (
    "MOZ_LOG",
    "MOZ_LOG_MODULES",
    "NSPR_LOG_MODULES",
    "MOZ_LOG_FILE",
    "NSPR_LOG_FILE",
)


def playwright_debug_on() -> bool:
    """Whether a Playwright debug log would carry what is typed: DEBUGP (the
    Python client prints every protocol message to stdout, the MCP transport),
    PWDEBUG, DEBUG_FILE, or a DEBUG pattern that could match any pw namespace
    (pw:channel logs every command with its arguments; pw:server:channel the
    commands the driver received). Playwright's namespaces are not a closed
    list, so any pattern with a wildcard counts, and so does one naming pw. A
    negation never clears it: refusing is the safe reading. Firefox's own
    logging counts too: any of its log variables set at all (its HTTP modules
    record what a page sends)."""
    if any(
        os.environ.get(v) is not None
        for v in ("DEBUGP", "PWDEBUG", "DEBUG_FILE", *_FIREFOX_LOG_VARS)
    ):
        return True
    return any(
        p and not p.startswith("-") and (p.startswith("pw") or any(c in p for c in "*?["))
        for p in re.split(r"[\s,]+", os.environ.get("DEBUG", ""))
    )


# Playwright reads a selector part as another engine than CSS when the text
# before its first "=" is an engine name (``internal:control=enter-frame``,
# ``xpath =//x``, ``*css=``: its parseSelectorString), when it is quoted
# (text), or when it starts with ``//``, ``(//`` or ``..`` (XPath); a ``>>``
# chain joins parts. A chain can step into a child frame, so the element found
# would not live in the frame whose origin the tap shows. Mirrored here so only
# plain CSS passes (an element in another frame is also refused on the page).
_ENGINE_NAME = re.compile(r"[A-Za-z0-9_+:*-]+")
_XPATH_START = re.compile(r"\(*//|\.\.")


def _plain_css(s: str) -> bool:
    if ">>" in s:
        return False
    part = s.strip()
    eq = part.find("=")
    if eq != -1 and _ENGINE_NAME.fullmatch(part[:eq].strip()):
        return False
    return not (part[:1] in ("'", '"') or _XPATH_START.match(part))


def _selector(s, code: str) -> str:
    if not isinstance(s, str) or not s.strip() or len(s) > MAX_SELECTOR or contains_card_number(s):
        raise Refused(code)
    if not s.isprintable() or not _plain_css(s):  # JS trim() strips a BOM strip() keeps
        raise Refused(code)
    return s


def _spec(spec, code: str) -> Spec:
    if isinstance(spec, dict):
        frames = spec.get("frames", [])
        if set(spec) - {"selector", "frames"} or not isinstance(frames, list):
            raise Refused(code)
        if len(frames) > MAX_FRAMES:
            raise Refused(code)
        return tuple(_selector(f, code) for f in frames), _selector(spec.get("selector"), code)
    return (), _selector(spec, code)


def _extra_value(v) -> str:
    if (
        not isinstance(v, str)
        or len(v) > MAX_EXTRA_VALUE
        or not v.isprintable()
        or holds_long_digit_run(v)
        or contains_card_number(v)
    ):
        raise Refused("bad_extra_fields")
    return v


def validate(card_ref, amount, fields, pay, expiry_format, extra_fields) -> CardArgs:
    """The checked arguments, or Refused with the first failing check's code."""
    if not isinstance(card_ref, str) or not CARD_REF.fullmatch(card_ref):
        raise Refused("bad_card_ref")
    if not (
        isinstance(amount, str)
        and 0 < len(amount) <= 32
        and amount.isprintable()
        and amount.strip()
        and any(c.isdigit() for c in amount)  # a price, not generic page text
        and not holds_long_digit_run(amount)
    ):
        raise Refused("bad_amount")
    if expiry_format not in EXPIRY_FORMATS:
        raise Refused("bad_expiry_format")
    if not isinstance(fields, dict) or not fields or not set(fields) <= set(CARD_KINDS):
        raise Refused("bad_fields")
    has_month, has_year = "exp_month" in fields, "exp_year" in fields
    if "number" not in fields or has_month != has_year or ("expiry" in fields and has_month):
        raise Refused("bad_fields")
    specs = {k: _spec(fields[k], "bad_fields") for k in CARD_KINDS if k in fields}
    if pay is None:
        raise Refused("bad_pay")
    pay_spec = _spec(pay, "bad_pay")
    extra_fields = {} if extra_fields is None else extra_fields
    if not isinstance(extra_fields, dict) or not set(extra_fields) <= set(FILL_NAMES):
        raise Refused("bad_extra_fields")
    extra = {}
    for name in FILL_NAMES:
        if name not in extra_fields:
            continue
        item = extra_fields[name]
        if name in specs or not isinstance(item, dict) or set(item) != {"selector", "value"}:
            raise Refused("bad_extra_fields")
        extra[name] = (_selector(item["selector"], "bad_extra_fields"), _extra_value(item["value"]))
    return CardArgs(card_ref, amount, specs, pay_spec, expiry_format, extra)


def reply_keys(kinds) -> set[str]:
    """The keys a claim reply holds for these field kinds (release.py hands
    back only the tapped fields, ``expiry`` as ``exp_month`` + ``exp_year``)."""
    keys = {"label", "last4"}
    for k in kinds:
        keys |= {"exp_month", "exp_year"} if k == "expiry" else {k}
    return keys


def values_for(card: dict, kinds, expiry_format: str) -> dict[str, str]:
    """kind -> the text to type (a select's option is matched from it)."""
    out = {}
    for kind in kinds:
        if kind in ("expiry", "exp_month", "exp_year"):
            mm = f"{int(card['exp_month']):02d}"
            yyyy = f"{int(card['exp_year']):04d}"
            yy = yyyy[-2:]
            if kind == "expiry":
                out[kind] = expiry_format.replace("MM", mm).replace("YYYY", yyyy).replace("YY", yy)
            else:
                out[kind] = mm if kind == "exp_month" else (yyyy if "YYYY" in expiry_format else yy)
        else:
            out[kind] = str(card[kind])
    return out


def value_landed(kind: str, sent: str, got) -> bool:
    """Whether the field now shows what was typed (a site may reformat digits)."""
    if not isinstance(got, str):
        return False
    got = got.strip()
    if kind == "exp_month":  # a select's option may drop the leading zero
        return re.fullmatch(r"\d{1,2}", got) is not None and int(got) == int(sent)
    if kind == "exp_year":  # or show the year in two or four digits
        full = lambda y: int(y) + 2000 if len(y) == 2 else int(y)  # noqa: E731
        return re.fullmatch(r"\d{2}|\d{4}", got) is not None and full(got) == full(sent)
    if kind in ("number", "cvc", "expiry"):  # a site may add spaces, dashes or a slash
        return re.fullmatch(r"[\d /-]*", got) is not None and re.sub(r"\D", "", got) == re.sub(
            r"\D", "", sent
        )
    return got.casefold() == sent.strip().casefold()


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    return " ".join("".join(" " if c.isspace() else c for c in text).split())


def amount_on_page(amount: str, text: str) -> bool:
    """Whether the stated amount appears in the page text as a whole number:
    ``19.99`` matches ``$19.99`` but not ``119.99``, ``19.990`` or ``1,19.99``."""
    a = _norm(amount)
    if not a:
        return False
    pattern = r"(?<!\d)(?<!\d[.,])" + re.escape(a) + r"(?!\d)(?![.,]\d)"
    return re.search(pattern, _norm(text)) is not None


def origin_of(url) -> str | None:
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


def secure_origin(origin) -> bool:
    """An https origin on a host the vault's release accepts: a dotted DNS name
    with a letter TLD. An IP, a single-label or a loopback host would only be
    refused later by the vault (``bad host``), after the checkout was set up."""
    if not isinstance(origin, str) or not origin.startswith("https://"):
        return False
    try:
        host = urlsplit(origin).hostname or ""
        return bool(host) and bool(normalise_merchant(host))  # the vault's own rule
    except (ValueError, VaultError):
        return False


def filter_state(state: dict, site: str, frame_origins: set[str]) -> dict:
    """The part of a ``storage_state`` the payment context starts with: cookies
    of the merchant's site and of the card frames' hosts, and the storage of
    origins on the merchant's site or exactly a card frame's origin."""
    hosts = {urlsplit(o).hostname for o in frame_origins}

    def cookie_ok(c) -> bool:
        d = str(c.get("domain", "")).lower().lstrip(".")
        return bool(d) and (
            domain_matches(d, site) or any(domain_matches(h, d) for h in hosts if h)
        )

    def origin_ok(o) -> bool:
        host = urlsplit(str(o.get("origin", ""))).hostname
        return o.get("origin") in frame_origins or bool(host and domain_matches(host, site))

    return {
        "cookies": [c for c in state.get("cookies", []) if isinstance(c, dict) and cookie_ok(c)],
        "origins": [o for o in state.get("origins", []) if isinstance(o, dict) and origin_ok(o)],
    }
