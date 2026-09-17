"""The login page is served as a Python STRING, and that is why it was missed.

Every other Genesis page is a template under `templates/*.html`, so anything that
enumerates pages by globbing that directory — a reviewer, a search, a lint — does
not see this one. It was overlooked twice while the same defect was being fixed
on the five pages that ARE templates.

The defect: `index.css` (inherited, loaded first by every page) pins
`html, body { overflow: hidden; position: fixed; height: 100% }` for an app-shell
layout. An ordinary document cannot scroll under it. MEASURED on the live login
page before the fix: `html` computed `overflow: hidden`, `position: fixed`, and
scrolling was unavailable. The login card fits a normal viewport, so nothing
looked wrong until the viewport was short or the error variant made the card
taller — which is exactly the shape that survives review.

The fix links the shared stylesheets instead of copying three declarations into
the page's own `<style>`. These tests pin that, and the one thing that would
silently undo it.
"""

from __future__ import annotations

import re

from genesis.dashboard import auth


def _links(html: str) -> list[str]:
    return re.findall(r'<link[^>]+href="([^"]+)"[^>]*>', html)


def test_the_login_page_links_the_shared_neutralisation():
    """Order matters as much as presence: the answer must load AFTER the leak."""
    hrefs = _links(auth._LOGIN_HTML)

    assert "/index.css" in hrefs, (
        "if the login page stops linking the vendor sheet there is nothing to "
        "neutralise — delete this test rather than weakening it"
    )
    assert "/css/components.css" in hrefs, (
        "components.css carries `html, body { overflow: auto !important; "
        "position: static !important }`; without it this page cannot scroll"
    )
    assert hrefs.index("/css/components.css") > hrefs.index("/index.css"), (
        "the neutralisation must load AFTER the sheet it neutralises"
    )
    assert "/css/tokens.css" in hrefs, "components.css is built on tokens.css's custom properties"


def test_the_login_stylesheets_are_not_behind_the_auth_gate():
    """The failure mode that would make the fix silently inert.

    `_check_auth` redirects an unauthenticated page request to `/genesis/login`.
    If a stylesheet the LOGIN page needs were itself gated, the browser would
    fetch a redirect instead of CSS — the page would render unstyled and
    unscrollable, and the only symptom would be visual. `/css/` is in the static
    allowlist today; this fails if that changes under the fix.
    """
    for href in _links(auth._LOGIN_HTML):
        assert any(href.startswith(prefix) for prefix in auth._STATIC_PREFIXES), (
            f"{href} is linked by the login page but is not in _STATIC_PREFIXES, "
            "so fetching it while logged out would redirect to the login page"
        )


def test_the_login_page_does_not_copy_the_neutralisation_inline():
    """Copy-per-page is what let this page be forgotten in the first place.

    Four pages each carry their own copy of the same three declarations, and the
    two that did not have them were invisible for exactly as long as nobody
    looked. A fifth copy here would pass a rendering check and reinstate the
    pattern, so the shared link is the thing worth pinning.
    """
    style = auth._LOGIN_HTML.split("<style>", 1)[1].split("</style>", 1)[0]
    assert "position: static" not in style and "overflow: auto" not in style, (
        "neutralise via the shared stylesheet, not a per-page copy — a copy here "
        "works and is how the next page gets missed"
    )
