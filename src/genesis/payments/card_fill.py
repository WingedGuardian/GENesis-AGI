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
from decimal import Decimal, InvalidOperation
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

CARD_KINDS = (
    "number",
    "expiry",
    "exp_month",
    "exp_year",
    "cvc",
    "name",
    "first_name",  # the sealed name's words but the last (split_name)
    "last_name",  # the sealed name's last word
    "postal_code",
)
# The kinds typed from the vault card's one sealed name.
NAME_KINDS = ("name", "first_name", "last_name")
EXPIRY_FORMATS = ("MM/YY", "MM / YY", "MM-YY", "MMYY", "MM/YYYY", "MMYYYY")
MAX_FRAMES = 4
# Clicks that bring the card form up in the payment window (a cart, a checkout
# button, an "add card" button): the most a checkout has needed is 3.
MAX_STEPS = 8
# Consent checkboxes ticked after the tap (a debit authorization in a card form).
MAX_CONSENTS = 2
MAX_SELECTOR = 500
MAX_EXTRA_VALUE = 256
# A vault refusal reason is one of release.py's fixed texts (lowercase words,
# no digits); anything else is not passed on.
_VAULT_REASON = re.compile(r"[a-z][a-z ,'()-]{0,99}")
_NONE = " Nothing was filled."
_CANCELLED = " The request was cancelled. Nothing was filled."
_SPENT = " A single-use card is now spent."
_NO_RETRY = " Outcome unknown: do NOT retry; the owner checks the merchant account or statement."
# After a save stage: the card went into the checkout's card form, Pay did not.
_SAVED = (
    " The card was handed to the checkout's card form (it may now be saved on the merchant "
    "account) and Pay was NOT clicked. The save control itself may have started a card check "
    "or a charge: tell the owner to look at the merchant account, and do not repeat the same "
    "steps blindly (the checkout may now show the saved card)."
)

TEXTS = {
    "card_fill_disabled": "Card fill is off (config/payments.yaml card_fill.enabled, or "
    "GENESIS_CARD_FILL_DISABLED=1). Hand the payment to the owner.",
    "playwright_debug_on": "Refused: Playwright or Firefox debug logging is on; it could log "
    "what is typed.",
    "bad_card_ref": "card_ref must be card_ followed by 12 hex characters.",
    "bad_amount": "amount must be the total as the page states it (1-32 printable characters, "
    "with its digits); with save, one price with its currency symbol ($19.99).",
    "bad_fields": "fields must map number (required), expiry or exp_month+exp_year, cvc, "
    "name or first_name+last_name, postal_code to a selector or {selector, frames}.",
    "bad_steps": f"steps must be a list of at most {MAX_STEPS} click targets, each a selector "
    "or {selector, frames}.",
    "bad_amount_max": "amount_max must be a price at or above amount, in the same currency "
    "(as the page writes prices), and needs save.",
    "bad_save": "save must be the control that stores the card, or {selector, frames}; total "
    "needs save.",
    "bad_consent": f"consent must be a list of at most {MAX_CONSENTS} checkboxes, each a "
    "selector or {selector, frames}.",
    "no_billing_value": "An extra field gives no value and card_fill.billing in the payments "
    "config has none for it.",
    "bad_pay": "pay must be the Pay control's selector, or {selector, frames}.",
    "bad_extra_fields": "extra_fields must map names from (" + ", ".join(FILL_NAMES) + ") "
    "to {selector, value} or {selector, frames, value}; no long digit run, no name already "
    "in fields.",
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
    "step_not_found": "A step did not match exactly one element in its frame." + _NONE,
    "step_failed": "A step's click failed." + _NONE,
    "step_is_consent": "A step ticked a checkbox (directly, through a label, or by a page "
    "script): pass it as consent, which is ticked only after the owner's tap." + _NONE,
    "step_is_submit": "A step targets a submit control or the Pay control: those are clicked "
    "only after the owner's tap." + _NONE,
    "consent_not_found": "A consent target is not exactly one checkbox in the top document or "
    "a card field's frame." + _NONE,
    "card_name_unusable": "The vault card's name cannot fill the name fields (empty, or one "
    "word for a first and last name); nothing was typed." + _SPENT,
    "consent_unlabelled": "A consent checkbox has no label the tap could show." + _NONE,
    "consent_label_too_long": "A consent checkbox's label is longer than the tap shows whole."
    + _NONE,
    "consent_pre_ticked": "A consent checkbox was already ticked before the tap (a default, or "
    "a step); only the approved fill ticks it." + _NONE,
    "consent_failed": "A consent checkbox did not take or keep its tick after the card was "
    "typed; Pay was NOT clicked and the payment window was closed." + _SPENT,
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
    "pay_get_form": "Refused: the pay control's form declares GET (card fields in a URL).",
    "extra_field_not_found": "An extra field selector did not match one input or select of "
    "its own.",
    "extra_field_origin": "A framed extra field is not in a frame that holds a card field.",
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
    "or tried to send a card value in a URL (and may have moved on with it). Treat the "
    "card as exposed and tell the owner." + _NO_RETRY,
    "card_value_in_request": "Pay was NOT clicked: while the card was typed, a page request "
    "carried a value equal to a typed CVC, expiry, name or postal code (possibly by "
    "coincidence) and was stopped. The card number was not seen leaving; the card is not "
    "necessarily exposed. Tell the owner." + _NO_RETRY,
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
    "carried a card value in a URL. Whether the checkout went ahead another way is "
    "unknown." + _NO_RETRY,
    "card_url_leaked": "Pay was clicked; a redirect carried (or may have carried) a card value "
    "in a URL, which the payment window could see but not stop." + _NO_RETRY,
    "outcome_unknown": "Pay was clicked, then the payment window, the browser or this call "
    "ended before the checkout settled." + _NO_RETRY,
    "vault_too_old": "The vault service did not show everything this payment needs on the "
    "tap (a ceiling, a save button or a consent box); restart genesis-server on current "
    "code." + _NONE,
    "save_not_found": "The save selector did not match exactly one button in the top "
    "document or a card field's frame." + _NONE,
    "total_not_shown": "No total appeared after the card was saved." + _SAVED,
    "total_over_ceiling": "The total after the card was saved is above amount_max." + _SAVED,
    "total_under_subtotal": "The total after the card was saved is below the stated amount."
    + _SAVED,
    "saved_page_changed": "The checkout changed after the card was saved (origin, title, Pay "
    "control, mask or a field)." + _SAVED,
    "save_challenge": "A bank check appeared when the card was saved." + _SAVED,
    "saved_stopped": "The flow stopped after the card was saved and before Pay." + _SAVED,
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
    extra: dict[str, tuple[Spec, str]]  # name -> (spec, value), in FILL_NAMES order
    steps: tuple[Spec, ...] = ()  # clicks, in order, before the form is read
    consent: tuple[Spec, ...] = ()  # checkboxes ticked after the tap
    # Extra fields whose value comes from the billing config: typed only after
    # the owner's tap, never into a page the owner has not approved.
    late: frozenset[str] = frozenset()
    save: Spec | None = None  # the control that stores the card, clicked before Pay
    amount_max: str | None = None  # the ceiling the tap approves ("up to")
    total: Spec | None = None  # where the total shows after the save (default: Pay's label)

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


def _payments_config() -> dict:
    """``config/payments.yaml`` plus its ``.local.yaml`` overlay; {} when unreadable."""
    from genesis._config_overlay import merge_local_overlay
    from genesis.env import repo_root

    path = repo_root() / "config" / "payments.yaml"
    try:
        base = yaml.safe_load(path.read_text()) if path.is_file() else {}
        cfg = merge_local_overlay(base if isinstance(base, dict) else {}, path, strict=True)
    except Exception as e:
        logger.warning("payments config unreadable (%s)", type(e).__name__)
        return {}
    return cfg if isinstance(cfg, dict) else {}


def billing_value(name: str) -> str:
    """``card_fill.billing.<name>`` (an extra field name) from the payments config's
    install overlay: the owner's billing details, kept out of tool arguments and
    public files. Refused when absent."""
    section = _payments_config().get("card_fill")
    billing = section.get("billing") if isinstance(section, dict) else None
    value = billing.get(name) if isinstance(billing, dict) else None
    if not isinstance(value, str) or not value.strip():
        raise Refused("no_billing_value")
    return value


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


def validate(
    card_ref,
    amount,
    fields,
    pay,
    expiry_format,
    extra_fields,
    steps=None,
    consent=None,
    save=None,
    amount_max=None,
    total=None,
) -> CardArgs:
    """The checked arguments, or Refused with the first failing check's code."""
    if not isinstance(card_ref, str) or not CARD_REF.fullmatch(card_ref):
        raise Refused("bad_card_ref")
    if not _price_text(amount):
        raise Refused("bad_amount")
    if expiry_format not in EXPIRY_FORMATS:
        raise Refused("bad_expiry_format")
    save_spec = None if save is None else _spec(save, "bad_save")
    total_spec = None if total is None else _spec(total, "bad_save")
    if total_spec is not None and save_spec is None:
        raise Refused("bad_save")
    # The total is checked against it, in its currency: one mark, as the page writes it.
    if save_spec is not None and (money(amount) is None or len(currency(amount)) != 1):
        raise Refused("bad_amount")
    if amount_max is not None:
        if not (_price_text(amount_max) and save_spec is not None):
            raise Refused("bad_amount_max")
        low, high = money(amount), money(amount_max)
        if low is None or high is None or high < low or currency(amount_max) != currency(amount):
            raise Refused("bad_amount_max")
    if not isinstance(fields, dict) or not fields or not set(fields) <= set(CARD_KINDS):
        raise Refused("bad_fields")
    has_month, has_year = "exp_month" in fields, "exp_year" in fields
    if "number" not in fields or has_month != has_year or ("expiry" in fields and has_month):
        raise Refused("bad_fields")
    if ("first_name" in fields) != ("last_name" in fields):  # half a name types half of it
        raise Refused("bad_fields")
    specs = {k: _spec(fields[k], "bad_fields") for k in CARD_KINDS if k in fields}
    if pay is None:
        raise Refused("bad_pay")
    pay_spec = _spec(pay, "bad_pay")
    steps = [] if steps is None else steps
    if not isinstance(steps, list) or len(steps) > MAX_STEPS:
        raise Refused("bad_steps")
    step_specs = tuple(_spec(st, "bad_steps") for st in steps)
    consent = [] if consent is None else consent
    if not isinstance(consent, list) or len(consent) > MAX_CONSENTS:
        raise Refused("bad_consent")
    consent_specs = tuple(_spec(c, "bad_consent") for c in consent)
    extra_fields = {} if extra_fields is None else extra_fields
    if not isinstance(extra_fields, dict) or not set(extra_fields) <= set(FILL_NAMES):
        raise Refused("bad_extra_fields")
    extra, late = {}, set()
    for name in FILL_NAMES:
        if name not in extra_fields:
            continue
        item = extra_fields[name]
        if (
            name in specs
            or not isinstance(item, dict)
            or "selector" not in item
            or set(item) - {"selector", "frames", "value"}
        ):
            raise Refused("bad_extra_fields")
        where = {k: item[k] for k in ("selector", "frames") if k in item}
        # No value: the owner's billing details in private config, never a public file.
        value = item["value"] if "value" in item else billing_value(name)
        if "value" not in item:
            late.add(name)
        extra[name] = (_spec(where, "bad_extra_fields"), _extra_value(value))
    return CardArgs(
        card_ref,
        amount,
        specs,
        pay_spec,
        expiry_format,
        extra,
        step_specs,
        consent_specs,
        frozenset(late),
        save_spec,
        amount_max,
        total_spec,
    )


def _price_text(value) -> bool:
    """A price as a page states it: 1-32 printable characters, with its digits."""
    return (
        isinstance(value, str)
        and 0 < len(value) <= 32
        and value.isprintable()
        and bool(value.strip())
        and any(c.isdigit() for c in value)  # a price, not generic page text
        and not holds_long_digit_run(value)
    )


# A price: digits with optional thousands commas and two decimals ($1,234.56,
# 285.46, 12). One number only; a text holding two prices is not one total.
_MONEY = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{2}))?(?![\d.,]*\d)")
# A price's text is short. A longer one is refused, never scanned: _MONEY's
# lookahead makes a long digit run cost quadratic time.
MONEY_MAX = 200
_SYMBOLS = "$\u20ac\u00a3\u00a5\u20b9"
# The currency a price is stated in: a symbol, or a code written beside it.
_CURRENCY = re.compile(rf"[{_SYMBOLS}]|\b(?:USD|EUR|GBP|JPY|CAD|AUD|CHF|CNY|INR|MXN)\b")
# A minus before the number, or between its symbol and the number: never a total.
_NEGATIVE = re.compile(rf"[-\u2212]\s?[{_SYMBOLS}]?\s?\d|[{_SYMBOLS}]\s?[-\u2212]\s?\d")


def currency(text) -> frozenset[str]:
    """The currency marks in a price's text ("$", "EUR"); empty when it has none."""
    return frozenset(_CURRENCY.findall(_norm(text))) if isinstance(text, str) else frozenset()


def money(text) -> Decimal | None:
    """The one price in ``text`` (units and cents), or None: none, more than one,
    a negative one, or a text too long to be a price."""
    if not isinstance(text, str) or len(text) > MONEY_MAX:
        return None
    norm = _norm(text)
    if re.search(r"\d \d", norm):  # digits grouped by spaces ("1 234,56"): not read, refused
        return None
    if _NEGATIVE.search(norm):
        return None
    found = _MONEY.findall(norm)
    if len(found) != 1:
        return None
    whole, cents = found[0]
    try:
        return Decimal(whole.replace(",", "") + "." + (cents or "00"))
    except InvalidOperation:
        return None


# What a Pay label may change between the tap and the click: its number. The
# currency symbol stays in the shape, so a label that switches currency differs.
_PRICE_TOKEN = re.compile(r"(?:\d[\d,]*(?:\.\d{2})?|-+\.-+)")


def label_shape(label) -> str:
    """A Pay label with its price masked: "Pay $--.--" and "Pay $285.46" match."""
    return _PRICE_TOKEN.sub("#", _norm(label)) if isinstance(label, str) else ""


def reply_keys(kinds) -> set[str]:
    """The keys a claim reply holds for these field kinds (release.py hands
    back only the tapped fields, ``expiry`` as ``exp_month`` + ``exp_year``)."""
    keys = {"label", "last4"}
    for k in kinds:
        if k == "expiry":
            keys |= {"exp_month", "exp_year"}
        else:
            keys.add("name" if k in NAME_KINDS else k)
    return keys


def split_name(name: str) -> tuple[str, str]:
    """(first, last): the last word is the last name, the words before it the
    first name (a middle name or initial stays with the first)."""
    words = name.split()
    return " ".join(words[:-1]), (words[-1] if words else "")


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
        elif kind in ("first_name", "last_name"):
            first, last = split_name(str(card["name"]))
            out[kind] = first if kind == "first_name" else last
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
