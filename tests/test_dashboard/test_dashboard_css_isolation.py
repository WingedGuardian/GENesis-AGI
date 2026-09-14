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

Comparing specificity then produced a hole of its own, and the shape is worth
keeping because it is easy to repeat: the first version scored every selector on
ONE bound and called erring high "the safe direction". It is safe on one side
only. A vendor rule scored too high is a false alarm someone reads; an ANSWER
scored too high is silence — the guard accepts an answer the browser will not
apply. The vendor side is now scored HIGH and the answer side LOW.

Declaring that asymmetry did not deliver it. A second audit constructed five
selectors whose LOW bound sat ABOVE the true CSS value, two of which accepted a
live leak end to end: a namespace prefix counted as a second element
(``svg|a``); an escaped character splitting one class name into two
(``.foo\\.bar``) or reading as a pseudo-class (``.md\\:flex``); a pseudo-class
inside an attribute VALUE (``[title=":hover"]``); and — needing no exotic CSS at
all — a selector GROUP scored at its strongest member on the answer side, where
``#app .panel, .panel`` answers at plain ``.panel`` for every panel outside
``#app``. All five are closed and each is a row in the table below. The lesson
is the one the earlier holes taught in a different costume: a stated safety
property is a claim to be attacked, not a design that holds because it was
written down.

SCOPE, stated rather than implied. This compares what is DECLARED, in what order
the sheets load, and a bounded specificity per rule. It does not compute the
cascade: within those bounds a functional pseudo-class is dropped rather than
resolved, shorthands are not expanded (``flex`` is not read as implying
``flex-direction``), and inline ``<style>`` blocks in a template are invisible.
It is a lint against one specific failure — a vendor layout declaration with no
Genesis answer — not a model of CSS.

And the bound is exact only for the selector shapes below it. Five over-counts
were found by construction rather than by reading; a sixth shape nobody has
written yet could over-count again, in the silent direction. Treat the
parametrised table as the shapes CHECKED, never as a proof about selectors in
general — which is the same distinction the corpus-versus-constructible one
makes everywhere else in this repository.

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


def _strip_parens(text: str) -> str:
    """Remove every parenthesised group, innermost first, including nesting."""
    while True:
        stripped = re.sub(r"\([^()]*\)", "", text)
        if stripped == text:
            return text
        text = stripped


def _selector_groups(prelude: list) -> list[str]:
    """Split a selector list on TOP-LEVEL commas only.

    `raw.split(",")` looks equivalent and is not: a comma inside a functional
    pseudo-class is an argument separator, not a selector separator, so
    `.panel:is(.a, .b)` split into `.panel:is(.a` and `.b)` — two nonsense
    selectors, one of which registered a spurious `.b` key while `.panel` was
    scored from a truncated string with its bounds collapsed. Found by a test
    written for something else; reading never would have, because the string
    split is the obvious thing and looks right.

    tinycss2 already hands back a token list in which the arguments of `:is(...)`
    live INSIDE a function block, so a top-level comma is the only kind visible
    here. Having parsed the stylesheet, this is the half that was still being
    pattern-matched.
    """
    groups: list[list] = [[]]
    for token in prelude:
        if token.type == "literal" and token.value == ",":
            groups.append([])
        else:
            groups[-1].append(token)
    return [s for s in (tinycss2.serialize(g).strip() for g in groups) if s]


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
    keys: set[str] = set()
    for selector in _selector_groups(prelude):
        # Drop the ARGUMENTS of every functional pseudo-class before looking for
        # the last compound. They contain whitespace and combinators of their
        # own, so `.panel:is(.a, .b)` split on whitespace yields `.b)` as the
        # "last compound" and the rule registers under `.b` while `.panel` — the
        # thing it actually targets — gets no key at all. The arguments never
        # change WHAT a compound matches, only how narrowly, so removing them is
        # exactly right for a key.
        selector = _strip_parens(selector)
        # Escapes, for the same reason as in `_specificity`: `.foo\.bar` is one
        # class named `foo.bar`, and splitting it would register an ANSWER under
        # `.foo` — a key it does not match, which reads as a clear.
        selector = re.sub(r"\\.", "", selector)
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


# Pseudo-classes whose specificity is NOT their own: `:where()` contributes
# nothing at all, and `:is()`/`:not()`/`:has()` contribute the specificity of
# their most specific argument rather than one class each.
_FUNCTIONAL_PSEUDO = re.compile(
    r":(?:where|is|not|has|matches|-moz-any|-webkit-any)\([^)]*\)", re.I
)


def _specificity(selector: str, important: bool, *, bound: str) -> tuple[int, int, int, int]:
    """A bounded CSS specificity, with `!important` sorting above everything.

    TWO bounds, because "err high" is only safe on one side and an earlier
    version of this function claimed it was safe on both. A vendor rule scored
    too high produces a false alarm someone reads; an ANSWER scored too high
    produces silence — the guard accepts an answer the browser will not apply.
    So the vendor side is scored HIGH and the answer side LOW, and every
    imprecision below becomes a false alarm rather than a false clear.

    What is imprecise: `:where()` contributes ZERO specificity in CSS, argument
    included, and `:is()`/`:not()`/`:has()` contribute the specificity of their
    most specific argument. Counting them as written — which the single-bound
    version did — over-scores both. Under `bound="low"` the whole functional
    pseudo-class is dropped; under `bound="high"` it is counted as written.
    Genesis stylesheets already use `:not()` on the answer side
    (`buttons.css`: `.btn-icon:hover:not(:disabled)`), so this is not
    hypothetical.

    Attribute operators are likewise counted as written, in both bounds, since
    an attribute selector is one class either way.

    Without any of this the guard compared only load ORDER, and an audit walked
    straight through it: `body .panel { display: flex }` added to the vendor
    sheet is more specific than a Genesis `.panel`, so it wins despite loading
    first — and the check passed, because the property was "answered later".
    """
    if bound == "low":
        selector = _FUNCTIONAL_PSEUDO.sub(" ", selector)
    else:
        # `:where()` is zero in BOTH bounds — that is exact, not an estimate.
        selector = re.sub(r":where\([^)]*\)", " ", selector, flags=re.I)

    # Three normalisations, each because the LOW bound was measured OVER the
    # true value without them — which is the one direction that turns a bound
    # into a false clear rather than a false alarm.
    #
    # Escapes: `.foo\.bar` is ONE class whose name contains a dot, and
    # `.md\:flex` is one class, not a class plus a pseudo-class. Dropping the
    # backslash AND the character it escapes stops either starting a new token.
    selector = re.sub(r"\\.", "", selector)
    # Attribute selectors are one class each, and their INTERIOR must not be
    # scanned: `[title=":hover"]` scored an extra pseudo-class for a string.
    attributes = len(re.findall(r"\[[^\]]*\]", selector))
    selector = re.sub(r"\[[^\]]*\]", " ", selector)
    # A namespace prefix is not an element: `svg|a` is one element, not two.
    selector = re.sub(r"(?:[A-Za-z0-9_-]+|\*)?\|", " ", selector)

    ids = len(re.findall(r"#[A-Za-z0-9_-]+", selector))
    classes = attributes + len(re.findall(r"\.[A-Za-z0-9_-]+", selector))
    # Pseudo-CLASSES count with classes; pseudo-ELEMENTS (::) count with elements.
    pseudo_el = len(re.findall(r"::[A-Za-z-]+", selector))
    classes += len(re.findall(r"(?<!:):[A-Za-z-]+(?:\([^)]*\))?", selector))
    stripped = re.sub(r"::?[A-Za-z-]+(?:\([^)]*\))?|[.#][A-Za-z0-9_-]+", " ", selector)
    elements = len(re.findall(r"(?<![\w-])[A-Za-z][A-Za-z0-9-]*", stripped))
    return (1 if important else 0, ids, classes, elements + pseudo_el)


def _declarations(css: str) -> dict[str, dict[str, tuple[bool, tuple, tuple]]]:
    """selector key -> {property: (declared_at_top_level, answer_spec, leak_spec)}.

    Three facts per declaration, each earned by a hole:

    * TOP LEVEL — a vendor declaration inside `@media` is still a leak, because
      it applies at some widths, so those are collected. A Genesis declaration
      inside `@media` is NOT an answer, because it does not apply at every
      width. The regex version counted both the same way and so handed the
      answer side a hiding place while its docstring claimed to close one.
    * ANSWER SPEC — the strongest specificity Genesis answers at, top level,
      scored at the LOW bound. Read when this sheet is the answer.
    * LEAK SPEC — the strongest the sheet declares at all, scored at the HIGH
      bound. Read when this sheet is the vendor, where a media-scoped rule
      still wins at the widths it applies to.

    The two bounds are not decoration: see `_specificity`. Scoring an answer
    high is how a guard accepts an answer the browser will not apply.
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
                groups = _selector_groups(node.prelude)
                for key in _selector_keys(node.prelude):
                    bucket = out.setdefault(key, {})
                    for d in decls:
                        # Score each comma-separated selector that produced this
                        # key and keep the strongest; a group is only as strong
                        # as its strongest member for the elements it matches.
                        matching = [
                            s
                            for s in groups
                            if key in _selector_keys(tinycss2.parse_component_value_list(s))
                        ] or groups
                        # MIN for the answer, MAX for the leak, and the
                        # asymmetry is the point. A group like
                        # `#app .panel, .panel` answers at its WEAKEST member for
                        # any `.panel` outside `#app`, so scoring it at the
                        # strongest accepted it against a vendor rule that beats
                        # the member actually applying. The same group as a
                        # VENDOR rule leaks if ANY member wins, so there the
                        # strongest is right. Taking the max on both sides read
                        # as symmetric and was backwards on one of them — this
                        # is ordinary CSS, no escapes or namespaces required.
                        low = min(_specificity(s, d.important, bound="low") for s in matching)
                        high = max(_specificity(s, d.important, bound="high") for s in matching)
                        was_top, ans_spec, leak_spec = bucket.get(d.lower_name, (False, zero, zero))
                        bucket[d.lower_name] = (
                            was_top or top_level,
                            max(ans_spec, low) if top_level else ans_spec,
                            max(leak_spec, high),
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


@pytest.mark.parametrize(
    ("selector", "low", "high"),
    [
        # Plain selectors: both bounds agree, because nothing is being estimated.
        ("body .panel", (0, 0, 1, 1), (0, 0, 1, 1)),
        (".panel", (0, 0, 1, 0), (0, 0, 1, 0)),
        ("#x .panel", (0, 1, 1, 0), (0, 1, 1, 0)),
        # `:where()` contributes ZERO, argument included — exact in BOTH bounds.
        (":where(body) .panel", (0, 0, 1, 0), (0, 0, 1, 0)),
        (".panel:where(.a.b.c)", (0, 0, 1, 0), (0, 0, 1, 0)),
        # `:is()`/`:not()`/`:has()` are estimated, so the bounds separate — and
        # they BRACKET the true value rather than merely differing. CSS scores
        # `.btn:not(:disabled)` at 2 classes and `.panel:is(.wide)` at 2; the
        # low bound sits at 1 for both, the high at 2 and 3. Measured against
        # the function rather than predicted: a first version of this table
        # guessed 3 for the `:not()` case, on the assumption that the inner
        # pseudo-class would be counted twice. It is not — the outer match
        # consumes it.
        (".btn:not(:disabled)", (0, 0, 1, 0), (0, 0, 2, 0)),
        (".panel:is(.wide)", (0, 0, 1, 0), (0, 0, 3, 0)),
        # Pseudo-ELEMENTS count as elements, and `::` must not read as two `:`.
        ("a::before", (0, 0, 0, 2), (0, 0, 0, 2)),
        # The one shape a Genesis sheet already uses, on the ANSWER side.
        (".btn-icon:hover:not(:disabled)", (0, 0, 2, 0), (0, 0, 3, 0)),
        # Four constructions where the LOW bound used to sit ABOVE the true CSS
        # value — the one direction that turns a bound into a false CLEAR. Each
        # is written with its true specificity in the comment; each was found by
        # an adversarial audit rather than by reading, and two of them produced
        # an end-to-end acceptance of a live leak.
        ("svg|a", (0, 0, 0, 1), (0, 0, 0, 1)),  # true (0,0,1): prefix is not an element
        (".foo\\.bar", (0, 0, 1, 0), (0, 0, 1, 0)),  # true (0,1,0): ONE escaped class name
        (".md\\:flex", (0, 0, 1, 0), (0, 0, 1, 0)),  # true (0,1,0): not a pseudo-class
        ('.panel[title=":hover"]', (0, 0, 2, 0), (0, 0, 2, 0)),  # true (0,2,0): value not scanned
    ],
)
def test_specificity_is_bounded_and_where_is_exactly_zero(selector, low, high):
    """The bounds must separate where the estimate is, and agree where it is not.

    `:where(body) .panel` is the case that matters and the one an external review
    named: CSS scores it identically to a bare `.panel`, so it must NOT satisfy a
    vendor `body .panel`. The single-bound version scored it HIGHER than
    `body .panel` and therefore accepted it as an answer — silence, in the one
    direction where silence is the failure.
    """
    assert _specificity(selector, False, bound="low") == low
    assert _specificity(selector, False, bound="high") == high
    assert _specificity(selector, False, bound="low") <= _specificity(
        selector, False, bound="high"
    ), "the low bound must never exceed the high one"


def test_a_where_wrapped_answer_does_not_satisfy_a_more_specific_vendor_rule():
    """The guard's own arithmetic, exercised end to end on constructed sheets.

    Asserting `_specificity` alone would leave the comparison unpinned: the
    function could be right and the caller could still read the wrong bound from
    each side, which is precisely the mistake the two bounds exist to prevent.
    """
    vendor = _declarations("body .panel { display: flex; }")
    weak = _declarations(":where(body) .panel { display: block; }")
    strong = _declarations("body .panel { display: block; }")

    leak = vendor[".panel"]["display"][2]  # HIGH bound — the vendor side
    assert weak[".panel"]["display"][1] < leak, (
        "a `:where()`-wrapped answer is no more specific than a bare class and "
        "must not be accepted against a descendant vendor selector"
    )
    assert strong[".panel"]["display"][1] >= leak, (
        "an equally specific answer must still be accepted, or the guard is "
        "merely noisy rather than correct"
    )


def test_an_answer_inflated_only_by_a_functional_pseudo_is_not_accepted():
    """Where the estimate is uncertain, the answer loses. On purpose.

    `:where()` is exact in both bounds, so the `:where()` test above cannot
    exercise the asymmetry — scoring the answer on the HIGH bound leaves it
    green, which a mutation showed. `:is()`/`:not()`/`:has()` are the estimated
    family, and this is what the two bounds actually buy: an answer whose
    specificity comes from an estimate is not accepted against a vendor rule of
    the SAME written form.

    The cost is stated rather than hidden: two identical selectors are reported
    as unanswered. That is a false alarm, and a false alarm is the price of
    never issuing a false clear on a value neither side can compute exactly.
    Silence a real one with a BENIGN entry and a reason.
    """
    vendor = _declarations(".panel:is(.a, .b) { display: flex; }")
    answer = _declarations(".panel:is(.a, .b) { display: block; }")

    leak = vendor[".panel"]["display"][2]
    assert answer[".panel"]["display"][1] < leak, (
        "an answer scored on the estimate's HIGH bound would be accepted here, "
        "which is the guard trusting a number it cannot compute"
    )
    # And the bounds must genuinely differ for this family, or the test above is
    # asserting nothing about the estimate.
    assert _specificity(".panel:is(.a, .b)", False, bound="low") < _specificity(
        ".panel:is(.a, .b)", False, bound="high"
    ), "the estimated family must separate the bounds, or there is no estimate"


def test_a_selector_GROUP_answers_at_its_weakest_member_and_leaks_at_its_strongest():
    """The asymmetry that reads as symmetric, and needs no exotic CSS at all.

    `#app .panel, .panel { display: block }` answers at `#app .panel` for any
    panel inside `#app` and at plain `.panel` everywhere else. A vendor
    `body .panel` beats the second member, so the answer does not cover every
    element the leak reaches — but scoring the group at its STRONGEST member
    accepted it. The same group as a VENDOR rule leaks if ANY member wins, so
    there the strongest IS right. Taking the max on both sides looked even-handed
    and was backwards on one of them.
    """
    group = "#app .panel, .panel { display: %s; }"
    answer = _declarations(group % "block")[".panel"]["display"][1]
    leak = _declarations(group % "flex")[".panel"]["display"][2]
    vendor = _declarations("body .panel { display: flex; }")[".panel"]["display"][2]

    assert answer == _specificity(".panel", False, bound="low"), (
        "an answer group is only as strong as its WEAKEST matching member"
    )
    assert leak == _specificity("#app .panel", False, bound="high"), (
        "a vendor group leaks at its STRONGEST matching member"
    )
    assert answer < vendor, (
        "so this group does not answer `body .panel` — which is what scoring it "
        "at the strongest member wrongly concluded"
    )
