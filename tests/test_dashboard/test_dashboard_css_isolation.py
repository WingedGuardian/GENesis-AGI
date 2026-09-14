"""Genesis pages load a vendor stylesheet they do not control.

Every page links ``/index.css`` — inherited Agent Zero CSS, written for AZ's own
side-by-side panes — before any Genesis stylesheet. Most of it is harmless base
styling. Its LAYOUT rules are not: they land on selectors Genesis also uses, and
a layout rule nobody overrides does not error, does not log, and does not fail a
test. It renders wrong, quietly, for months.

That has now happened three times:

* ``html, body { overflow: hidden; position: fixed; height: 100% }`` made a page
  unscrollable. Someone hit it, wrote the ``!important`` neutralisation, and
  moved on — then the next page hit it and wrote its own copy, and so did the
  next. Four copies, in ``dashboard.css`` and three inline ``<style>`` blocks.
* The fifth page never got a copy. MEASURED on a page carrying the same
  stylesheet set: disable that block and the body is ``position: fixed``,
  ``overflow: hidden``, and the document cannot scroll at all. Copy-per-page is
  how the page that forgot went unnoticed.
* ``.panel { display: flex; height: 100% }`` turned every panel header into a
  narrow vertical gutter down the left of its panel, with the title floating
  level with the middle of the body. MEASURED in a browser 2026-09-13: the
  Zero-Drop header box was 261x882 px and System Health's 170x2501, inside
  panels ~1350 px wide. Nobody hit it, because nothing breaks — it only looks
  wrong, and looking is the one check with no harness.

So this does not pin ``.panel``. It pins the CLASS, from two directions:

* per page — for every selector a vendor sheet and a Genesis sheet both style,
  any layout property the vendor sets must be answered by a Genesis sheet that
  loads LATER, at the top level, or be listed in ``BENIGN`` with a reason;
* across pages — an answer that exists on one page must exist on all of them,
  which is the asymmetry the third incident above actually was.

Polarity is ALLOWLIST. A denylist of known-bad selectors would have passed
cleanly for the whole time ``.panel`` was broken, because nobody knew to add it.

WHAT AN EARLIER VERSION OF THIS FILE GOT WRONG, kept because every hole was a
way of claiming more than it checked. An adversarial audit constructed seven live
leaks and the first version passed five of them:

* It skipped, rather than failed, when no Genesis sheet declared the selector
  LATER. Reversing two ``<link>`` tags therefore made every leak on the page
  invisible and the suite green — while reintroducing the original bug in full.
  Load order is the invariant this file is about; it is now an assertion.
* It matched selectors by exact string, so ``body .panel`` missed ``.panel`` —
  and a descendant selector is precisely the shape that would BEAT a
  same-specificity Genesis answer.
* Its regex CSS parser counted a Genesis answer inside ``@media`` as an answer,
  and dropped any rule containing a brace inside a ``url()`` or a string.
* Its regex link parser required ``rel`` before ``href``, so it silently returned
  7 of the dashboard's 8 stylesheet links.

Three of those were fixed by parsing rather than pattern-matching: ``tinycss2``
for CSS, stdlib ``html.parser`` for the link tags. A hand-rolled parser inside a
guard fails OPEN in the shapes its author did not think of, which is what they
all were.

The fourth, ``body .panel``, survived the rewrite too — it collided correctly and
then read as ANSWERED, because comparing load order says nothing about which rule
wins. Specificity is now compared as well: an answer must load later AND be at
least as specific. That was the last of the seven to fall, and it fell to a
mutation rather than to reading.

SCOPE, stated rather than implied. This compares what is DECLARED, in what order
the sheets load, and a rough specificity per rule. It does not compute the
cascade. The specificity is approximate — ``:is()``/``:where()`` argument lists,
``:not()`` contents and attribute operators are counted as written rather than
resolved, deliberately erring HIGH so a rule is never scored weaker than it is.
Shorthands are not expanded (``flex`` is not read as implying
``flex-direction``), and inline ``<style>`` blocks in a template are invisible.
It is a lint against one specific failure — a vendor layout declaration with no
Genesis answer — not a model of CSS.

Two gaps worth naming, because a guard is read as covering what it does not say
it misses. The per-page test only fires where Genesis has ALREADY staked a layout
claim on the selector; a leak onto a selector Genesis never lays out is invisible
to it, which is the price of not drowning in ``a``, ``ul`` and ``input``. The
cross-page test covers part of that gap and not all of it — it needs the answer
to exist SOMEWHERE. A leak nobody has ever answered on any page is outside both,
and neither of these tests found the third incident above; a person did.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
import tinycss2

ROOT = Path(__file__).resolve().parents[2]
WEBUI = ROOT / "src/genesis/dashboard/webui"
TEMPLATE_DIR = ROOT / "src/genesis/dashboard/templates"

# Genesis owns /css/*. Everything else a page links is inherited or vendored.
OWNED_PREFIX = "/css/"

# Properties whose value decides where a box IS, rather than what it looks like.
# A wrong colour is visible to whoever wrote it; a wrong `display` moves other
# people's content. Deliberately narrow: the set that has actually bitten here,
# plus the near neighbours of those two incidents.
LAYOUT_PROPS = frozenset(
    {
        "display",
        "position",
        "float",
        "flex-direction",
        "grid-template-columns",
        "height",
        "width",
        "overflow",
    }
)

# Vendor layout declarations that need no Genesis answer, each with the reason it
# is inert. A row is only as good as its last read — keep this table small, and
# prefer answering a leak in a Genesis sheet over explaining it away here.
#
# Every entry below is the same declaration, `width: 100%`, listed per selector
# rather than by dropping `width` from LAYOUT_PROPS: what makes them inert is the
# VALUE being 100% under a global `* { box-sizing: border-box }` (index.css:4-8),
# which is a fact about these declarations and not about the property. A vendor
# sheet that sets `width: 250px` on a shared selector is a real leak and must
# still fail.
BENIGN: dict[tuple[str, str], str] = {
    ("html", "width"): (
        "`width: 100%` on the root block box is what it already does; margin is "
        "0 on the same rule, so there is nothing for the percentage to change."
    ),
    ("body", "width"): (
        "Same as `html` — a block-level child of <html> already fills it, and "
        "the shared rule zeroes margin and padding."
    ),
    (".section", "width"): (
        "`width: 100%` on a block box, and border-box means the padding and "
        "border modals.css adds sit INSIDE the 100% rather than overflowing it."
    ),
}


class _LinkCollector(HTMLParser):
    """Stylesheet hrefs in document order, indifferent to attribute order.

    The regex this replaces required `rel` before `href` and so missed
    `<link href="…" rel="stylesheet">` — one of the dashboard's eight links,
    silently, while its docstring claimed to return them all.
    """

    def __init__(self) -> None:
        super().__init__()
        self.hrefs: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "link":
            return
        a = {k.lower(): (v or "") for k, v in attrs}
        if "stylesheet" in a.get("rel", "").lower().split() and a.get("href"):
            self.hrefs.append(a["href"])


def _linked_stylesheets(template: Path) -> list[str]:
    parser = _LinkCollector()
    parser.feed(template.read_text())
    return parser.hrefs


def _selector_keys(prelude: list) -> set[str]:
    """Approximate which elements a selector can match, as a set of keys.

    The exact-string comparison this replaces treated `.panel` and `body .panel`
    as unrelated, which is backwards: a descendant selector is MORE specific, so
    it beats a Genesis `.panel` answer rather than missing it.

    CLASS keys come from the LAST compound and ignore everything before it, so
    `.panel`, `body .panel`, `div.panel` and `.wrap > .panel` all yield
    `{".panel"}` and collide. A class name is a shared namespace: whoever writes
    `.panel` is reaching for the same elements no matter what they scope it
    under, and the descendant form is the one that WINS on specificity.

    ELEMENT keys are far stricter — emitted only when the whole selector is a
    bare element name, optionally with pseudo-classes. `html, body { … }` counts;
    `.switch input` and `input[type="range"]` do not. Reducing those to `input`
    reported two findings where the only thing the sheets shared was the tag
    name, and a widget's own scoped rule can never reach Genesis markup. The two
    real incidents are both in scope under this rule: `.panel` is a class, and
    `html, body` is bare.

    It still over-matches on classes, deliberately — `.panel.wide` yields
    `.panel` too, so a rule that applies only to elements carrying both classes
    is reported. A guard that errs toward reporting can be silenced with a
    BENIGN entry and a stated reason; one that errs toward silence cannot be
    noticed at all.
    """
    text = tinycss2.serialize(prelude).strip()
    keys: set[str] = set()
    for selector in text.split(","):
        selector = selector.strip()
        if not selector:
            continue
        # Last compound: everything after the final combinator or whitespace.
        last = re.split(r"[\s>+~]+", selector)[-1]
        # Drop pseudo-classes/elements — they narrow, they do not re-target.
        bare = re.split(r"::?", last)[0]
        classes = re.findall(r"\.([A-Za-z0-9_-]+)", bare)
        if classes:
            keys.update("." + c for c in classes)
            continue
        if re.fullmatch(r"[A-Za-z][A-Za-z0-9-]*", selector.split(":")[0].strip()):
            keys.add(bare)
    return keys


def _specificity(selector: str, important: bool) -> tuple[int, int, int, int]:
    """A rough CSS specificity, with `!important` sorting above everything.

    Rough on purpose and in the safe direction: `:is()`/`:where()` argument
    lists, `:not()` contents and attribute operators are counted as written
    rather than resolved, so a selector is never scored LOWER than it really is.
    A vendor rule scored too high produces a false alarm someone reads; one
    scored too low produces silence nobody reads.

    Without this the guard compared only load ORDER, and an audit walked
    straight through it: `body .panel { display: flex }` added to the vendor
    sheet is more specific than a Genesis `.panel`, so it wins despite loading
    first — and the check passed, because the property was "answered later".
    """
    ids = len(re.findall(r"#[A-Za-z0-9_-]+", selector))
    classes = len(re.findall(r"\.[A-Za-z0-9_-]+|\[[^\]]*\]", selector))
    # Pseudo-CLASSES count with classes; pseudo-ELEMENTS (::) count with elements.
    pseudo_el = len(re.findall(r"::[A-Za-z-]+", selector))
    classes += len(re.findall(r"(?<!:):[A-Za-z-]+(?:\([^)]*\))?", selector))
    stripped = re.sub(r"::?[A-Za-z-]+(?:\([^)]*\))?|\[[^\]]*\]|[.#][A-Za-z0-9_-]+", " ", selector)
    elements = len(re.findall(r"(?<![\w-])[A-Za-z][A-Za-z0-9-]*", stripped))
    return (1 if important else 0, ids, classes, elements + pseudo_el)


def _declarations(css: str) -> dict[str, dict[str, tuple[bool, tuple, tuple]]]:
    """selector key -> {property: (declared_at_top_level, best_top_spec, best_spec)}.

    Three facts per declaration, each earned by a hole:

    * TOP LEVEL — a vendor declaration inside `@media` is still a leak, because
      it applies at some widths, so those are collected. A Genesis declaration
      inside `@media` is NOT an answer, because it does not apply at every
      width. The regex version counted both the same way and so handed the
      answer side a hiding place while its docstring claimed to close one.
    * BEST TOP SPEC — the strongest specificity Genesis answers at, top level.
    * BEST SPEC — the strongest the sheet declares at all, used for the vendor
      side, where a media-scoped rule still wins at the widths it applies to.
    """
    out: dict[str, dict[str, tuple[bool, tuple, tuple]]] = {}
    zero = (0, 0, 0, 0)

    def walk(nodes: list, top_level: bool) -> None:
        for node in nodes:
            if node.type == "qualified-rule":
                decls = [
                    d
                    for d in tinycss2.parse_blocks_contents(node.content)
                    if d.type == "declaration"
                ]
                if not decls:
                    continue
                raw = tinycss2.serialize(node.prelude).strip()
                for key in _selector_keys(node.prelude):
                    bucket = out.setdefault(key, {})
                    for d in decls:
                        # Score each comma-separated selector that produced this
                        # key and keep the strongest; a group is only as strong
                        # as its strongest member for the elements it matches.
                        spec = max(
                            (
                                _specificity(s, d.important)
                                for s in raw.split(",")
                                if key in _selector_keys(tinycss2.parse_component_value_list(s))
                            ),
                            default=_specificity(raw, d.important),
                        )
                        was_top, top_spec, any_spec = bucket.get(d.lower_name, (False, zero, zero))
                        bucket[d.lower_name] = (
                            was_top or top_level,
                            max(top_spec, spec) if top_level else top_spec,
                            max(any_spec, spec),
                        )
            elif node.type == "at-rule" and node.content is not None:
                # @media / @supports wrap ordinary rules; @keyframes wraps step
                # rules whose "selectors" are `from`/`to`/percentages, which are
                # not elements. Skipping it keeps those out of the key space.
                if node.lower_at_keyword in {"media", "supports", "layer", "container"}:
                    walk(tinycss2.parse_rule_list(node.content), top_level=False)

    walk(tinycss2.parse_stylesheet(css, skip_whitespace=True, skip_comments=True), True)
    return out


def _resolve(href: str) -> Path | None:
    """Map a served href to its file under webui/, or None if we do not ship it."""
    candidate = WEBUI / href.lstrip("/")
    return candidate if candidate.is_file() else None


def _templates_linking_stylesheets() -> list[Path]:
    return sorted(p for p in TEMPLATE_DIR.glob("*.html") if _linked_stylesheets(p))


TEMPLATES = _templates_linking_stylesheets()


def test_the_template_scan_found_the_pages():
    """Guard the guard: an empty or shrinking population makes everything pass."""
    assert len(TEMPLATES) >= 5, (
        f"expected at least the five top-level pages, found {len(TEMPLATES)} — "
        "the link parse or the glob is broken, not the CSS"
    )
    names = {p.name for p in TEMPLATES}
    assert "genesis_dashboard.html" in names and "genesis_voice.html" in names, (
        "the two pages that use `.panel` must both be in scope"
    )
    # The dashboard's eight links include one written `href` before `rel`; the
    # regex this replaced returned seven and said nothing.
    dash = _linked_stylesheets(TEMPLATE_DIR / "genesis_dashboard.html")
    raw = (TEMPLATE_DIR / "genesis_dashboard.html").read_text().count('rel="stylesheet"')
    assert len(dash) == raw, f"parsed {len(dash)} stylesheet links, source has {raw}"


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_no_vendor_layout_rule_is_left_unanswered(template: Path):
    """Every vendor layout declaration on a shared selector has a later answer.

    Fails the way the `.panel` defect should have failed: naming the page, the
    selector and the property, before anyone has to look at the rendered page.
    """
    hrefs = _linked_stylesheets(template)
    order = {h: i for i, h in enumerate(hrefs)}
    owned = [h for h in hrefs if h.startswith(OWNED_PREFIX) and _resolve(h)]
    foreign = [h for h in hrefs if not h.startswith(OWNED_PREFIX) and _resolve(h)]
    if not owned or not foreign:
        pytest.skip(f"{template.name} links no owned/vendor pair we ship")

    owned_decls = {h: _declarations(_resolve(h).read_text()) for h in owned}
    problems: list[str] = []

    for f_href in foreign:
        for sel, f_props in _declarations(_resolve(f_href).read_text()).items():
            for prop in sorted(set(f_props) & LAYOUT_PROPS):
                if (sel, prop) in BENIGN:
                    continue
                # Only a selector Genesis LAYS OUT can be leaked into. Two
                # separate narrowings, and the second is what keeps this usable:
                #
                #   * a selector Genesis never mentions is the vendor's business;
                #   * a selector Genesis styles WITHOUT any layout property is
                #     also the vendor's business. A base stylesheet setting
                #     `display` on `a` or `ul` is doing its job, and Genesis
                #     giving that element a colour is not a competing claim.
                #
                # Without the second, this reported nine findings across the five
                # pages, six of them element selectors where the only overlap was
                # that both sheets mention the tag. The signal is Genesis having
                # expressed layout intent on the selector and the vendor winning
                # anyway — which is exactly the shape of both real incidents:
                # `.panel` (Genesis lays it out in components.css) and
                # `html, body` (Genesis lays them out in dashboard.css).
                styling = [
                    h
                    for h in owned
                    if sel in owned_decls[h] and (set(owned_decls[h][sel]) & LAYOUT_PROPS)
                ]
                if not styling:
                    continue
                vendor_spec = f_props[prop][2]
                # An answer must load LATER and be at least as SPECIFIC. Order
                # alone let an audit through: `body .panel { display: flex }` in
                # the vendor sheet outranks a Genesis `.panel` and wins despite
                # loading first, while the property still read as "answered".
                later = [
                    h
                    for h in styling
                    if order[h] > order[f_href]
                    and owned_decls[h][sel].get(prop, (False, None, None))[0]
                    and owned_decls[h][sel][prop][1] >= vendor_spec
                ]
                if later:
                    continue
                outgunned = [
                    h
                    for h in styling
                    if order[h] > order[f_href]
                    and owned_decls[h][sel].get(prop, (False, None, None))[0]
                ]
                earlier = [h for h in styling if order[h] < order[f_href]]
                media_only = [
                    h for h in styling if order[h] > order[f_href] and prop in owned_decls[h][sel]
                ]
                if outgunned:
                    best = max(owned_decls[h][sel][prop][1] for h in outgunned)
                    why = (
                        f"answered in {outgunned}, but the vendor selector is MORE "
                        f"specific ({vendor_spec} vs {best}) and wins despite "
                        "loading first"
                    )
                elif media_only:
                    why = f"answered in {media_only} but only inside an at-rule"
                elif earlier:
                    why = f"styled in {earlier}, which loads EARLIER — the vendor rule wins"
                else:
                    why = "no Genesis sheet declares it"
                problems.append(f"{f_href} sets `{prop}` on `{sel}` — {why}")

    assert not problems, (
        f"{template.name}: a vendor stylesheet's layout rule wins on a selector "
        "Genesis styles:\n  "
        + "\n  ".join(sorted(set(problems)))
        + "\n\nDeclare the property at the TOP LEVEL of a Genesis stylesheet "
        "that loads LATER, or add it to BENIGN with the reason it is inert."
    )


def test_the_panel_leak_that_motivated_this_is_answered_on_every_page_that_uses_it():
    """The specific instance, pinned so the class test cannot go vacuous on it.

    The class test passes if `.panel` stops being shared — remove Genesis's
    `.panel` rule entirely and the selector no longer collides, so the leak
    sails through. This asserts the answer exists, and that it lives on a sheet
    every affected page actually loads.
    """
    vendor = _declarations((WEBUI / "index.css").read_text())
    assert "display" in vendor.get(".panel", {}), (
        "this test is about answering index.css's `.panel { display: flex }`; if "
        "the vendor rule is gone the answer can go too, and so can this test"
    )

    answer = _declarations((WEBUI / "css/components.css").read_text()).get(".panel", {})
    assert answer.get("display", (False,))[0] and answer.get("height", (False,))[0], (
        "components.css must declare `display` and `height` for `.panel` at the "
        "top level — a panel is a header ABOVE a body, and inheriting AZ's flex "
        "lays the two out side by side"
    )

    # The page that would have been missed: it uses `.panel` and never loads
    # dashboard.css, which is where the fix was first (wrongly) written.
    voice = _linked_stylesheets(TEMPLATE_DIR / "genesis_voice.html")
    assert "/css/components.css" in voice and "/css/dashboard.css" not in voice, (
        "genesis_voice.html is the reason the answer lives in components.css; if "
        "its stylesheet list changed, re-check where the answer belongs"
    )
    assert '<div class="panel"' in (TEMPLATE_DIR / "genesis_voice.html").read_text(), (
        "and the reason is that it USES `.panel` — if it stopped, say so here"
    )


def test_a_vendor_rule_answered_on_one_page_is_answered_on_every_page():
    """Cross-page consistency — the hole the per-page test cannot see.

    The per-page test only fires when Genesis has already staked a layout claim
    on the selector: a vendor rule on a selector Genesis never lays out is the
    vendor's business, and without that narrowing the check drowns in `a`, `ul`
    and `input`. The cost is precise — it cannot notice a page where Genesis
    staked NO claim and should have.

    That is not hypothetical. It is how the voice page was left with
    `html, body { position: fixed; overflow: hidden }` from index.css while four
    other pages neutralised it, three of them by copying the same three
    declarations into their own inline <style>. MEASURED on a page with the same
    stylesheet set: disabling that block gives `position: fixed`,
    `overflow: hidden`, and a document that cannot scroll.

    So this asks the question the other test cannot: if Genesis answers a vendor
    layout property ANYWHERE, every page carrying that vendor rule must answer it
    too. An asymmetry is either a page that was forgotten or an answer that
    belongs on a shared sheet.

    Its own blind spot, stated: inline <style> blocks in a template are invisible
    here, so a page answering a leak inline reads as unanswered. Today that is
    only ever a FALSE ALARM, never a false clear, which is the safe direction —
    and the fix for one is to move the answer onto a shared sheet, which is what
    this test wants anyway.
    """
    # (selector, property) pairs some Genesis sheet answers at top level
    answered_somewhere: set[tuple[str, str]] = set()
    for sheet in (WEBUI / "css").glob("*.css"):
        for sel, props in _declarations(sheet.read_text()).items():
            for prop, (top_level, _, _) in props.items():
                if top_level and prop in LAYOUT_PROPS:
                    answered_somewhere.add((sel, prop))

    gaps: list[str] = []
    for template in TEMPLATES:
        hrefs = _linked_stylesheets(template)
        order = {h: i for i, h in enumerate(hrefs)}
        owned = [h for h in hrefs if h.startswith(OWNED_PREFIX) and _resolve(h)]
        foreign = [h for h in hrefs if not h.startswith(OWNED_PREFIX) and _resolve(h)]
        if not owned or not foreign:
            continue
        owned_decls = {h: _declarations(_resolve(h).read_text()) for h in owned}
        for f_href in foreign:
            for sel, f_props in _declarations(_resolve(f_href).read_text()).items():
                for prop in sorted(set(f_props) & LAYOUT_PROPS):
                    if (sel, prop) not in answered_somewhere or (sel, prop) in BENIGN:
                        continue
                    if any(
                        order[h] > order[f_href]
                        and owned_decls[h].get(sel, {}).get(prop, (False, None, None))[0]
                        for h in owned
                    ):
                        continue
                    gaps.append(
                        f"{template.name}: {f_href} sets `{prop}` on `{sel}`, which "
                        "Genesis answers on other pages but not on this one"
                    )

    assert not gaps, (
        "a vendor layout rule is neutralised on some pages and not others:\n  "
        + "\n  ".join(sorted(set(gaps)))
        + "\n\nMove the answer to a stylesheet every affected page loads "
        "(css/components.css) rather than answering it per page."
    )
