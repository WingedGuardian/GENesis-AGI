"""The two declarations that undo the inherited viewport and panel layout.

`index.css` is inherited from another project and loads first on every page. Two
of its rules are written for an app shell and are wrong for an ordinary document:

    body, html { overflow: hidden; position: fixed; height: 100% }
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
exists, on the sheet every page loads, after the sheet it answers, and it says
what it has to say.

WHAT THE EXTRACTOR BELOW DOES AND DOES NOT MODEL, stated because the first two
versions of this file both claimed more than they performed. It resolves, for one
selector and one property, the declaration a browser would use IF no other
selector also matched the element: comments removed, at-rules removed, every
top-level rule whose selector list CONTAINS that selector considered in document
order, last declaration winning. It knows nothing about specificity, about a
different selector that happens to match the same element, or about other sheets.
A rule reached only through a MORE specific selector is therefore invisible here,
by choice — that is the cascade model this file refuses to carry.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

WEBUI = Path(__file__).resolve().parents[2] / "src/genesis/dashboard/webui"
TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "src/genesis/dashboard/templates"

PANEL = ".panel"
ROOT = "html, body"

# The vendor declarations these answer. Pinned by value, so a vendor sheet that
# stops carrying the leak fails LOUDLY here rather than leaving the answer below
# pinning a neutralisation of nothing.
VENDOR_PANEL = {"display": "flex", "height": "100%"}
VENDOR_ROOT = {"overflow": "hidden", "position": "fixed", "height": "100%"}


def _strip_comments(css: str) -> str:
    """Remove `/*…*/` everywhere, FIRST, before anything else looks at the text.

    An earlier version stripped comments inside the declaration reader but not in
    the rule finder that chose which block to read. MEASURED: commenting the real
    `.panel` rule out above a broken one left all seven tests green, reading the
    answer out of the comment. "Comment the old block out while writing the new
    one" is an ordinary edit, so that is not a theoretical hole.
    """
    return re.sub(r"/\*.*?\*/", "", css, flags=re.S)


def _strip_at_rules(css: str) -> str:
    """Remove every at-rule, so only unconditional top-level rules remain.

    A rule inside `@media` applies at some widths and therefore does not ANSWER a
    vendor rule that applies at all of them. An earlier version claimed a
    line-start anchor excluded those — it does not, because a nested rule can
    start at column 0, and a mutation that moved the real answer inside a
    never-matching `@media (min-width: 99999px)` left every assertion green. The
    docstring asserted the exclusion; nothing performed it.

    Both FORMS are handled. A blocked at-rule (`@media …{…}`) is skipped to its
    matching brace; a BLOCKLESS one (`@import url(x);`) ends at its semicolon, and
    an earlier version that assumed a block ate the next real rule instead.
    """
    out, depth, i = [], 0, 0
    while i < len(css):
        ch = css[i]
        if ch == "@" and depth == 0:
            j = i
            while j < len(css) and css[j] not in "{;":
                j += 1
            if j >= len(css):
                break  # unterminated at-rule: nothing after it is a rule either
            if css[j] == ";":
                i = j + 1
                continue
            at_depth = 0
            while j < len(css):
                if css[j] == "{":
                    at_depth += 1
                elif css[j] == "}":
                    at_depth -= 1
                    if at_depth == 0:
                        break
                j += 1
            i = j + 1
            continue
        depth += ch == "{"
        depth -= ch == "}"
        out.append(ch)
        i += 1
    return "".join(out)


def _selector_parts(raw: str) -> frozenset[str]:
    """A selector list as a set of whitespace-normalised parts.

    A set, so `body, html` and `html, body` are the same selector — the vendor
    writes the root one way and we write it the other, and an earlier version
    carried a helper whose whole job was papering over that, while leaving our own
    side pinned to one spelling that a formatter run would have broken.

    Commas inside `:is(…)`/`:where(…)` do not separate parts, so the split is
    paren-depth aware. Neither sheet uses those today; the lint this file was
    split from got exactly this wrong, which is why it is handled here.
    """
    parts: list[str] = []
    depth, cur = 0, []
    for ch in raw:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    parts.append("".join(cur))
    return frozenset(" ".join(p.split()) for p in parts if p.strip())


_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")


def _rules(css: str) -> list[tuple[frozenset[str], str]]:
    """Every unconditional top-level rule, in document order."""
    body = _strip_at_rules(_strip_comments(css))
    return [(_selector_parts(m.group(1)), m.group(2)) for m in _RULE_RE.finditer(body)]


def _declaration(block: str, prop: str) -> tuple[str, bool] | None:
    """The winning value of `prop` within ONE block, and whether it is important.

    Last declaration wins, which is what the cascade does inside a block — the
    vendor's own `.panel` relies on it, declaring `display` twice so that
    `-webkit-flex` loses to `flex`.

    Property names, values and `!important` are all case-insensitive in CSS, so
    the comparison is folded. A longhand is NOT a match for its shorthand:
    `overflow-y` does not answer `overflow`.
    """
    values = re.findall(r"(?:^|;)\s*" + re.escape(prop) + r"\s*:([^;]+)", block, re.I)
    if not values:
        return None
    raw = values[-1].strip()
    important = bool(re.search(r"!\s*important$", raw, re.I))
    return re.sub(r"!\s*important$", "", raw, flags=re.I).strip().casefold(), important


def _declared(css: str, selector: str, prop: str) -> tuple[str, bool] | None:
    """The declaration a browser would use for `selector`, or None.

    EVERY matching rule is considered, not the first one found. An earlier version
    used a single `re.search` and returned the first textual match, so APPENDING

        .panel { display: flex; height: 100% }

    to the answering sheet restored the exact defect with all seven tests green —
    the later rule wins at equal specificity, and the guard could not see it.

    A rule whose selector list CONTAINS ours counts: `.panel, .card {…}` answers
    for `.panel`. A rule covering only PART of ours does not — `body {…}` alone is
    not an answer for `html, body`, and splitting our own rule that way would turn
    these red. That is the conservative direction, and it is stated here rather
    than left to be discovered.
    """
    wanted = _selector_parts(selector)
    for parts, block in reversed(_rules(css)):
        if wanted <= parts:
            hit = _declaration(block, prop)
            if hit is not None:
                return hit
    return None


def _components() -> str:
    return (WEBUI / "css/components.css").read_text()


def _vendor() -> str:
    return (WEBUI / "index.css").read_text()


@pytest.mark.parametrize(
    "selector,expected",
    [(PANEL, VENDOR_PANEL), (ROOT, VENDOR_ROOT)],
    ids=["panel", "root"],
)
def test_the_vendor_sheet_still_has_the_rules_these_answer(selector, expected):
    """Guard the guard. If the leak is gone, the answer can go, and so can this.

    Pinned by VALUE, not by property name. The answer below is only correct
    RELATIVE to what the vendor sets; if the vendor stops setting `display: flex`
    the right move is a decision about deleting the answer, and this is the test
    that should force it rather than one further down failing obscurely.
    """
    vendor = _vendor()
    for prop, value in expected.items():
        assert _declared(vendor, selector, prop) == (value, False), (
            f"index.css no longer sets `{prop}: {value}` on `{selector}` — the "
            "answer in components.css may now be unnecessary, or may be answering "
            "the wrong value. Decide that before touching the tests below"
        )


@pytest.mark.parametrize("prop,expected", [("display", "block"), ("height", "auto")])
def test_components_css_answers_the_panel_layout(prop, expected):
    """The VALUE, not just the property name.

    An earlier version asserted only that the property appeared. MEASURED: with
    `.panel` set back to the vendor's own `display: flex; height: 100%` — the exact
    defect, byte for byte — all seven tests here passed. A presence check cannot
    fail on the thing it exists to catch.
    """
    answer = _declared(_components(), PANEL, prop)
    assert answer is not None, (
        f"components.css must declare `{prop}` on `{PANEL}` at the top level — a "
        "panel is a header ABOVE a body, and the inherited flex lays the two out "
        "side by side"
    )
    value, _ = answer
    assert value == expected, (
        f"`{PANEL}` must end up at `{prop}: {expected}`, not `{value}` — that is "
        "the declaration that puts the header back above the body"
    )
    assert value != VENDOR_PANEL[prop], (
        f"the answer restates the vendor's own `{prop}: {value}`, which answers "
        "nothing. If you changed the stylesheet, change it back; if the VENDOR "
        "changed, the guard test above is the one to read first"
    )


@pytest.mark.parametrize(
    "prop,expected", [("overflow", "auto"), ("position", "static"), ("height", "auto")]
)
def test_components_css_answers_the_viewport_lock(prop, expected):
    """Same shape, and the `!important` is checked PER PROPERTY.

    The block-wide `"!important" in root` this replaces was satisfied by any one
    of the three carrying it.
    """
    answer = _declared(_components(), ROOT, prop)
    assert answer is not None, f"`{ROOT}` must answer `{prop}`"
    value, important = answer
    assert value == expected, (
        f"`{ROOT}` must end up at `{prop}: {expected}`, not `{value}` — the vendor "
        "pins the page root, and restating its value leaves the page unscrollable"
    )
    assert important, (
        f"`{prop}` must carry `!important` — the vendor rule sets it on the same "
        "selector, so equal specificity is not enough"
    )
    assert value != VENDOR_ROOT[prop], (
        f"the answer restates the vendor's own `{prop}: {value}`, which answers "
        "nothing. If you changed the stylesheet, change it back; if the VENDOR "
        "changed, the guard test above is the one to read first"
    )


@pytest.mark.parametrize(
    "prop,longhand",
    [
        ("overflow", "overflow-x"),
        ("overflow", "overflow-y"),
        ("height", "min-height"),
        ("height", "max-height"),
    ],
)
def test_no_longhand_takes_the_viewport_lock_back(prop, longhand):
    """A longhand declared alongside the shorthand can undo it silently.

    MEASURED against the previous version: adding `overflow-y: hidden !important`
    to the answering block left every test green over a page that cannot scroll,
    because nothing looked for a property it had not been told to check.

    Stricter than the cascade requires — a longhand declared BEFORE the shorthand
    is harmless — but components.css declares none of these on the page root, so
    the strict form costs nothing and needs no ordering model to be right.
    """
    assert _declared(_components(), ROOT, longhand) is None, (
        f"`{longhand}` on `{ROOT}` overrides the `{prop}` answer for the axis it "
        "covers; put the value in the shorthand instead"
    )


def test_every_page_links_the_sheet_carrying_the_answer():
    """An answer only reaches a page that asks for it.

    Both pages that were missed were missed this way, and the second of them is
    not a template at all — the login page is a Python string, so a check that
    globs `templates/*.html` cannot see it. It is enumerated explicitly here for
    that reason.

    The glob is NON-RECURSIVE, so a page added under `templates/partials/` or any
    other subdirectory is invisible to it. Nothing under those directories links a
    stylesheet today (MEASURED: `href="/index.css"` appears on exactly six pages,
    the five top-level templates and the login string), and a page added later is
    covered only when someone links the sheet.
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
