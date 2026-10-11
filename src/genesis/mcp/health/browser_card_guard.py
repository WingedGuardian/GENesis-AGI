"""Keep payment card numbers out of the general browser tools.

What a refusal here stops: the card being typed into the page, written to
``mcp_health.log`` (``browser_run_js`` logs its expression), and anything the
page then does with it. It cannot un-send a value the model already put in a
tool argument: that argument is in the transcript before the tool runs. The
refusal steers the session to the vault's approved card-fill flow instead.

The checks: a value or script that contains a card number, a target field that
takes one, and keystrokes while focus sits in such a field.

Focus is found by following ``document.activeElement`` down through open shadow
roots and frames: a shadow host is replaced by its shadow root's active element,
and when the focused element is an iframe its content frame is entered and the
walk repeats. That works for cross-origin payment iframes, which Playwright can
evaluate in (MEASURED in Camoufox, headed and headless). ``document.hasFocus()``
is not used, because a browser on a virtual display does not report window
focus reliably.

When the walk cannot finish (a frame detached, an evaluation failed or hung,
an iframe with no reachable frame, a custom element whose shadow root is closed
or absent so its inside cannot be read), the answer is "unverified", and
callers treat that as a refusal for anything that would type characters.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from genesis.security.card_shapes import is_card_field

logger = logging.getLogger(__name__)

REFUSE_VALUE = (
    "Refused: the value looks like a payment card number. Card numbers are never "
    "typed with the general browser tools; card entry goes through the vault's "
    "approved card-fill flow."
)
REFUSE_FIELD = (
    "Refused: the target is a payment card field (number, security code or "
    "expiry). The general browser tools never type into card fields; card entry "
    "goes through the vault's approved card-fill flow."
)
REFUSE_UNVERIFIED = (
    "Refused: focus is inside an embedded frame or component that could not be "
    "checked, so it may be a payment card field. Only navigation keys (Tab, Enter, arrows, "
    "Escape, Backspace) are allowed there; card entry goes through the vault's "
    "approved card-fill flow."
)
REFUSE_TARGET_UNVERIFIED = (
    "Refused: the target field could not be checked for being a payment card "
    "field (it did not resolve in time, or could not be read). Retry once the "
    "page has finished loading."
)
REFUSE_RUN_JS = (
    "Refused: the script contains what looks like a payment card number. Card "
    "numbers never go through browser_run_js; card entry goes through the vault's "
    "approved card-fill flow."
)

# Keys that move focus or edit without typing a character, so they are safe
# even while focus is in a card field. A closed set: any other key is refused
# there. Playwright key names are case-sensitive, so the match is exact.
KEYS_ALLOWED_IN_CARD_FIELD = frozenset(
    {
        "Tab",
        "Shift+Tab",
        "Enter",
        "Escape",
        "ArrowLeft",
        "ArrowRight",
        "ArrowUp",
        "ArrowDown",
        "Backspace",
        "Delete",
        "Home",
        "End",
        "Space",
    }
)

# One evaluation of the focused element returns in milliseconds; a step still
# running after 5 s belongs to a hung frame, and the walk reports unverified
# rather than stalling the tool call behind it.
_STEP_TIMEOUT_S: float = 5.0
# Payment pages nest at most two or three frames deep; a longer chain is not a
# real page, and the walk stops rather than loop.
_MAX_FRAME_DEPTH = 8

# The attributes is_card_field reads, from one element.
_FIELD_ATTRS_JS = """(el) => {
  if (!el || !el.getAttribute) return {};
  let label = null;
  try {
    if (el.labels && el.labels.length) label = el.labels[0].innerText;
  } catch (e) {}
  return {
    tag: (el.tagName || '').toLowerCase(),
    autocomplete: el.getAttribute('autocomplete'),
    name: el.getAttribute('name'),
    id: el.id || null,
    aria_label: el.getAttribute('aria-label'),
    placeholder: el.getAttribute('placeholder'),
    label: label,
  };
}"""

# The deepest focused element: through open shadow roots, stopping at an
# iframe (entered separately, through its content frame).
_DEEP_ACTIVE_JS = """() => {
  let el = document.activeElement;
  while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
  return el;
}"""

# The element's tag, and whether its inside is unreadable: a custom element
# with no open shadow root (closed, or none we can see into).
_TAG_INFO_JS = """(el) => {
  const tag = el && el.tagName ? el.tagName.toLowerCase() : '';
  return {tag: tag, opaque: tag.includes('-') && !el.shadowRoot};
}"""


class CardFieldRefused(Exception):
    """Typing was refused because focus is (or may be) in a card field."""


def key_allowed_in_card_field(key: str) -> bool:
    return key in KEYS_ALLOWED_IN_CARD_FIELD


async def _step(awaitable, timeout: float | None = None):
    # The module bound is read at call time, so a test that lowers it reaches here.
    return await asyncio.wait_for(
        awaitable, timeout=_STEP_TIMEOUT_S if timeout is None else timeout
    )


async def target_is_card_field(page, selector: str) -> bool | None:
    """Whether ``selector``'s first match is a card field; None if it can't be read."""
    try:
        # Playwright's own timeout ends its wait for a missing element; the
        # outer bound is only a backstop. Cancelling from outside alone left
        # Playwright's wait running until the page closed (MEASURED, Camoufox).
        attrs = await _step(
            page.locator(selector).first.evaluate(_FIELD_ATTRS_JS, timeout=_STEP_TIMEOUT_S * 1000),
            timeout=_STEP_TIMEOUT_S + 1,
        )
    except Exception:  # noqa: BLE001 - an unreadable target is "unknown", decided by the caller
        logger.debug("card guard: target attributes unreadable", exc_info=False)
        return None
    return is_card_field(attrs or {})


async def focused_card_field(page) -> bool | None:
    """Whether the focused element, through shadow roots and frames, is a card field.

    True: it is. False: it is verifiably not (including nothing focused).
    None: the walk could not finish, so it may be.
    """
    frame = page.main_frame
    try:
        for _ in range(_MAX_FRAME_DEPTH):
            handle = await _step(frame.evaluate_handle(_DEEP_ACTIVE_JS))
            try:
                element = handle.as_element()
                if element is None:
                    return False
                info = await _step(element.evaluate(_TAG_INFO_JS)) or {}
                if info.get("tag") in ("iframe", "frame"):
                    child = await _step(element.content_frame())
                    if child is None:
                        return None
                    frame = child
                    continue
                attrs = await _step(element.evaluate(_FIELD_ATTRS_JS))
                if is_card_field(attrs or {}):
                    return True
                # A custom element we cannot see inside may hold a card field.
                return None if info.get("opaque") else False
            finally:
                # Disposal is cleanup only, but it is bounded like every other
                # step: a wedged transport must not hang the probe.
                with contextlib.suppress(Exception):
                    await _step(handle.dispose())
    except Exception:  # noqa: BLE001 - any failure leaves the answer unknown
        return None
    return None


async def refuse_if_focused_card_field(page) -> None:
    """Raise ``CardFieldRefused`` unless focus is verifiably not in a card field."""
    focused = await focused_card_field(page)
    if focused is True:
        raise CardFieldRefused(REFUSE_FIELD)
    if focused is None:
        raise CardFieldRefused(REFUSE_UNVERIFIED)
