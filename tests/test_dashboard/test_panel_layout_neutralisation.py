"""The two declarations that undo the inherited viewport and panel layout.

`index.css` is inherited from another project and loads first on every page. Two
of its rules are written for an app shell and are wrong for an ordinary document:

    html, body { overflow: hidden; position: fixed; height: 100% }
    .panel     { display: flex; height: 100% }

The first makes a page unscrollable. The second lays a panel's header BESIDE its
body instead of above it — MEASURED in a browser before the fix, the Zero-Drop
header box was 261x882 px inside a panel ~1350 px wide, putting the title some
440 px below the top of the box it names.

Neither errors and neither logs. Three separate pages carried one or the other
for months, each found by a person looking at a page.

These tests pin the ANSWER and nothing else. They deliberately do not model the
cascade: a general lint that does was built alongside this fix, was found
fail-open in four consecutive reviews, and lives on its own branch until it
converges. What is here instead is the narrow, checkable thing — the answer
exists, on the sheet every page loads, after the sheet it answers.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEBUI = Path(__file__).resolve().parents[2] / "src/genesis/dashboard/webui"
TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "src/genesis/dashboard/templates"


def _strip_at_rules(css: str) -> str:
    """Remove every `@…{…}` block, so only top-level rules remain.

    A rule inside `@media` applies at some widths and therefore does not ANSWER
    a vendor rule that applies at all of them. The first version of this file
    claimed a line-start anchor was enough to exclude those — it is not, because
    a nested rule can perfectly well start at column 0, and a mutation that moved
    the real answer inside a never-matching `@media (min-width: 99999px)` left
    every assertion here green. The docstring asserted the exclusion; nothing
    performed it. Brace depth is what actually decides it.
    """
    out, depth, i = [], 0, 0
    while i < len(css):
        ch = css[i]
        if ch == "@" and depth == 0:
            j, at_depth, seen = i, 0, False
            while j < len(css):
                if css[j] == "{":
                    at_depth += 1
                    seen = True
                elif css[j] == "}":
                    at_depth -= 1
                    if at_depth == 0:
                        break
                j += 1
            if seen:
                i = j + 1
                continue
        depth += ch == "{"
        depth -= ch == "}"
        out.append(ch)
        i += 1
    return "".join(out)


def _block(css: str, selector: str) -> str:
    """The declaration block of a TOP-LEVEL rule, or '' if it has none."""
    match = re.search(
        r"(?:^|\})\s*" + re.escape(selector) + r"\s*\{([^{}]*)\}",
        _strip_at_rules(css),
        re.M,
    )
    return match.group(1) if match else ""


def test_the_vendor_sheet_still_has_the_rules_these_answer():
    """Guard the guard. If the leak is gone, the answer can go, and so can this.

    Without this the tests below would keep passing over a neutralisation of
    nothing — green, and pinning a rule nobody needs.
    """
    vendor = (WEBUI / "index.css").read_text()
    panel = _block(vendor, ".panel")
    assert "display" in panel and "height" in panel, (
        "index.css no longer lays out `.panel`; re-check whether the answer in "
        "components.css is still needed before deleting these tests"
    )
    root = _block(vendor, "body,\nhtml") or _block(vendor, "body, html")
    assert "position" in root and "overflow" in root, (
        "index.css no longer pins the page root; same question"
    )


@pytest.mark.parametrize("prop", ["display", "height"])
def test_components_css_answers_the_panel_layout(prop):
    panel = _block((WEBUI / "css/components.css").read_text(), ".panel")
    assert prop in panel, (
        f"components.css must declare `{prop}` on `.panel` at the top level — a "
        "panel is a header ABOVE a body, and the inherited flex lays the two out "
        "side by side"
    )


@pytest.mark.parametrize("prop", ["overflow", "position", "height"])
def test_components_css_answers_the_viewport_lock(prop):
    root = _block((WEBUI / "css/components.css").read_text(), "html,\nbody")
    assert root, "components.css must carry a top-level `html, body` rule"
    assert f"{prop}:" in root and "!important" in root, (
        f"`{prop}` must be answered, and with `!important` — the vendor rule "
        "sets these on the same selector, so equal specificity is not enough"
    )


def test_every_page_links_the_sheet_carrying_the_answer():
    """An answer only reaches a page that asks for it.

    Both pages that were missed were missed this way, and the second of them is
    not a template at all — the login page is a Python string, so a check that
    globs `templates/*.html` cannot see it. It is enumerated explicitly here for
    that reason; a page added later is covered only when someone links the sheet.
    """
    from genesis.dashboard import auth

    pages = {p.name: p.read_text() for p in TEMPLATE_DIR.glob("*.html")}
    pages["auth.py::_LOGIN_HTML"] = auth._LOGIN_HTML

    missing = []
    for name, html in pages.items():
        hrefs = re.findall(r'<link[^>]+href="([^"]+)"', html)
        if "/index.css" not in hrefs:
            continue  # no vendor sheet, nothing to answer
        if "/css/components.css" not in hrefs:
            missing.append(name)
        elif hrefs.index("/css/components.css") < hrefs.index("/index.css"):
            missing.append(f"{name} (links it BEFORE index.css)")

    assert not missing, (
        "these pages load the vendor sheet without the answer, so they carry the "
        f"viewport lock: {missing}"
    )
