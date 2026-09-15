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
  any layout property the vendor sets must be answered by a Genesis rule on THE
  SAME SELECTOR, at the top level, in a sheet that loads LATER, and carrying
  ``!important`` if the vendor declaration does — or be listed in ``BENIGN``
  with a reason;
* across pages — an answer that exists on one page must exist on all of them,
  which is the asymmetry the third incident above actually was.

Polarity is ALLOWLIST. A denylist of known-bad selectors would have passed
cleanly for the whole time ``.panel`` was broken, because nobody knew to add it.

WHAT EARLIER VERSIONS OF THIS FILE GOT WRONG, kept because every hole was a way
of claiming more than it checked. An adversarial audit constructed seven live
leaks and the first version passed five of them:

* It skipped, rather than failed, when no Genesis sheet declared the selector
  LATER. Reversing two ``<link>`` tags therefore made every leak on the page
  invisible and the suite green — while reintroducing the original bug in full.
* It matched selectors by exact string, so ``body .panel`` missed ``.panel``.
* Its regex CSS parser counted a Genesis answer inside ``@media`` as an answer,
  and dropped any rule containing a brace inside a ``url()`` or a string.
* Its regex link parser required ``rel`` before ``href``, so it silently returned
  7 of the dashboard's 8 stylesheet links.

Three of those were fixed by parsing rather than pattern-matching: ``tinycss2``
for CSS, stdlib ``html.parser`` for the link tags. A hand-rolled parser inside a
guard fails OPEN in the shapes its author did not think of, which is what they
all were.

THE FOURTH IS WHY THIS FILE NOW COMPARES SELECTORS FOR IDENTITY RATHER THAN
MODELLING THE CASCADE, and the history is kept because the model looked more
rigorous at every step while the holes stayed the same size.

Matching ``.panel`` against ``body .panel`` needed a rule for which one WINS, so
specificity was compared. Specificity then turned out to answer the wrong
question — it says who wins where both rules apply and nothing about WHERE, so
``.panel:hover`` was accepted while every panel nobody was pointing at still took
the vendor declaration. A coverage test was added on top: the answer must apply
everywhere the leak does, then beat it there.

Both were estimates, and both were attacked successfully. The specificity
estimate over-counted in five separately-constructed selector shapes — a
namespace prefix, two escape forms, a pseudo-class inside an attribute value, and
a selector group scored at its strongest member — two of which accepted a live
leak end to end. The coverage test compared flat token SETS, which cannot see
position or multiplicity, so ``.panel .panel``, ``.panel + .panel``,
``.panel > .panel`` and ``#chat.panel`` all read as covering ``.panel``:
substituting the first of those for the shipped answer reintroduced the original
incident with 26 tests green.

Four consecutive reviews each found the model fail-open in a new shape, and the
shapes had nothing in common except that a person had to think of them. So the
model is gone. An answer must now be written on the VENDOR'S OWN SELECTOR, byte
for byte after normalising whitespace and group order. Two rules with the same
selector apply to exactly the same elements at exactly the same specificity, so
neither question needs estimating and the remaining two — which sheet loads
later, and which declaration is ``!important`` — are facts.

WHAT THAT COSTS, stated plainly because it is the whole trade. A genuinely
covering answer written any other way now reads as UNANSWERED. That is a false
alarm: someone reads the failure, sees the answer is real, and adds a ``BENIGN``
row with the reason. The failure it replaces was silence, and silence is what
let all three incidents ship.

SCOPE, stated rather than implied. This compares what is DECLARED and in what
order the sheets load. It does not compute the cascade: shorthands are not
expanded (``flex`` is not read as implying ``flex-direction``), a selector that
is not identical is not analysed at all, and inline ``<style>`` blocks in a
template are invisible. It is a lint against one specific failure — a vendor
layout declaration with no Genesis answer — not a model of CSS. It also does not
check the VALUE: a Genesis rule restating the vendor's own value is still an
answer, because what this file asserts is that a Genesis DECISION exists on the
declaration. Whether a particular answer neutralises the leak it names is pinned
beside that answer, in ``test_panel_layout_neutralisation.py``.

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
        "grid-template-rows",
        "height",
        "min-height",
        "max-height",
        "width",
        "min-width",
        "max-width",
        "overflow",
        "overflow-x",
        "overflow-y",
    }
)

# At-rules whose contents cannot target elements, so not descending into them
# loses nothing. Anything NOT here and not descended into raises, because the
# alternative is a whole block of rules reading as absent. `@keyframes` wraps
# step rules whose "selectors" are `from`/`to`/percentages; the rest wrap
# descriptors or nothing at all.
_AT_RULES_WITHOUT_ELEMENTS = frozenset(
    {
        "keyframes",
        "-webkit-keyframes",
        "-moz-keyframes",
        "-o-keyframes",
        "font-face",
        "counter-style",
        "font-feature-values",
        "font-palette-values",
        "property",
        "page",
        "viewport",
        "-ms-viewport",
    }
)

# Vendor layout declarations that need no Genesis answer, each with the reason it
# is inert. A row is only as good as its last read — keep this table small, and
# prefer answering a leak in a Genesis sheet over explaining it away here.
#
# KEYED ON THE VALUE, because the value is what makes these inert and the
# property is not. An earlier version keyed `(selector, property)` while the
# comment beside it said, correctly, that a vendor sheet setting `width: 250px`
# on a shared selector "is a real leak and must still fail". MEASURED: changing
# the vendor's `width: 100%` to `width: 250px` left all 23 tests green — a
# 250px-wide document root on all five pages, cleared by a row written for a
# different number. A clearance is for a DECLARATION, never for a slot.
BENIGN: dict[tuple[str, str, str], str] = {
    ("html", "width", "100%"): (
        "`width: 100%` on the root block box is what it already does; margin is "
        "0 on the same rule, so there is nothing for the percentage to change."
    ),
    ("body", "width", "100%"): (
        "Same as `html` — a block-level child of <html> already fills it, and "
        "the shared rule zeroes margin and padding."
    ),
    ("html", "min-width", "320px"): (
        "A FLOOR, not a lock: it sets a minimum the viewport already exceeds on "
        "every device this dashboard is used on, and a floor cannot stop the "
        "document scrolling or move a box that is wider. Unlike the `overflow` "
        "and `position` declarations in the same rule, which are answered."
    ),
    ("body", "min-width", "320px"): ("Same rule, same reason as `html`."),
    ("html", "min-height", "370px"): (
        "Also a floor. It can make a SHORT document taller than its content, "
        "which is cosmetic, and cannot clip or lock anything — the property that "
        "did that, `height: 100%`, is answered in components.css."
    ),
    ("body", "min-height", "370px"): ("Same rule, same reason as `html`."),
    (".section", "width", "100%"): (
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
    return [s for s in (_normalise(g) for g in groups) if s]


def _normalise(tokens: list) -> str:
    """Selector text in a normal form, so identity means what it claims to.

    `_answers` compares selector TEXT, so two selectors that select the same
    elements must produce the same string or a real answer reads as absent. Two
    ways they differ without differing:

    * TYPE NAMES ARE ASCII CASE-INSENSITIVE in HTML (Selectors 4 §6.1). MEASURED
      before this existed: rewriting the vendor's own `body,\\nhtml {` as
      `BODY,\\nHTML {` left the full `overflow: hidden; position: fixed` viewport
      lock in place with all 23 tests green — the second of the two incidents
      this file exists for, reproduced with the shift key. Class and id names are
      case-SENSITIVE and are deliberately not folded, which is why this walks
      tokens rather than lowercasing the serialised string.
    * WHITESPACE AROUND A COMBINATOR is insignificant, so `.wrap>.panel` and
      `.wrap > .panel` are one selector written twice. A minified vendor sheet
      writes the first and any hand-written answer writes the second. That one
      fails CLOSED — a false alarm rather than a leak — but the module docstring
      claimed both were normalised while only end-stripping happened.
    """
    out: list[str] = []
    for i, token in enumerate(tokens):
        if token.type == "whitespace":
            out.append(" ")
            continue
        text = tinycss2.serialize([token])
        # An ident is a TYPE name only when nothing binds it to a class, id or
        # pseudo. `.Panel` and `#App` keep their case; `DIV` does not.
        prev = tinycss2.serialize([tokens[i - 1]]) if i else ""
        if token.type == "ident" and prev not in {".", "#", ":", "::"}:
            text = text.lower()
        out.append(text)
    return re.sub(r"\s*([>+~])\s*", r"\1", "".join(out)).strip()


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


def _declarations(css: str) -> dict[str, dict[str, list[dict]]]:
    """selector key -> property -> the RULES declaring it.

    Per RULE, and each rule is recorded once per GROUP MEMBER, because answering
    is judged on the selector a member actually writes. `body, html { … }` is two
    members and answers a vendor `html` rule; `#app .panel, .panel { … }` is two
    members and answers a vendor `.panel` rule through its second one only.

    Each rule carries:
      sel        the normalised selector text of the member that produced it
      top        declared at the top level, not inside an at-rule. A vendor rule
                 inside `@media` still leaks at the widths it applies to; a
                 Genesis rule inside one does not answer at every width.
      important  carries `!important`.
    """
    out: dict[str, dict[str, list[dict]]] = {}

    def walk(nodes: list, top_level: bool) -> None:
        for node in nodes:
            if node.type == "qualified-rule":
                contents = tinycss2.parse_blocks_contents(node.content)
                nested = [n for n in contents if n.type == "qualified-rule"]
                if nested:
                    raise AssertionError(
                        f"`{tinycss2.serialize(node.prelude).strip()}` contains a "
                        "NESTED rule. This guard reads declarations only, so a rule "
                        "nested inside another is invisible to it and every leak in "
                        "it reads as absent. MEASURED: moving the vendor's `.panel` "
                        "rule inside one left all 23 tests green. Flatten it, or "
                        "teach `walk` to resolve `&` against the parent prelude."
                    )
                decls = [d for d in contents if d.type == "declaration"]
                if not decls:
                    continue
                groups = _selector_groups(node.prelude)
                for key in _selector_keys(node.prelude):
                    bucket = out.setdefault(key, {})
                    matching = [
                        s
                        for s in groups
                        if key in _selector_keys(tinycss2.parse_component_value_list(s))
                    ] or groups
                    for d in decls:
                        rules = bucket.setdefault(d.lower_name, [])
                        for sel in matching:
                            rules.append(
                                {
                                    "sel": sel,
                                    "top": top_level,
                                    "important": d.important,
                                    "value": tinycss2.serialize(d.value).strip(),
                                }
                            )
            elif node.type == "at-rule" and node.content is not None:
                # @media / @supports wrap ordinary rules; @keyframes wraps step
                # rules whose "selectors" are `from`/`to`/percentages, which are
                # not elements. Skipping it keeps those out of the key space.
                if node.lower_at_keyword in {"media", "supports", "layer", "container"}:
                    walk(tinycss2.parse_rule_list(node.content), top_level=False)
                elif node.lower_at_keyword not in _AT_RULES_WITHOUT_ELEMENTS:
                    raise AssertionError(
                        f"`@{node.lower_at_keyword}` wraps rules this guard does not "
                        "descend into, so every rule inside it is invisible — which "
                        "is a clear, not a pass. Add it to the descend list if it "
                        "wraps ordinary rules, or to _AT_RULES_WITHOUT_ELEMENTS with "
                        "the reason its contents cannot target elements."
                    )

    walk(tinycss2.parse_stylesheet(css, skip_whitespace=True, skip_comments=True), True)
    return out


def _rules(decls: dict, key: str, prop: str) -> list[dict]:
    return decls.get(key, {}).get(prop, [])


def _answers(answer_rules: list[dict], leak: dict) -> bool:
    """Is this leak answered — same selector, top level, and not out-ranked?

    EXACT SELECTOR IDENTITY, which is the whole redesign. The version this
    replaces asked whether the answer COVERED the vendor selector, computed from
    a flat set of the tokens each one contained. Position and multiplicity are
    invisible to a set, so `.panel .panel`, `.panel + .panel`, `.panel > .panel`
    and `#chat.panel` all read as covering `.panel` — and substituting the first
    of those for the shipped answer reintroduced the original incident with 26
    tests green. That model also needed a specificity comparison to decide who
    won where both applied, and the estimate behind it over-counted in five
    separately-constructed shapes, two of which accepted a live leak.

    Identity needs neither. Two rules with the same selector text apply to
    exactly the same elements and at exactly the same specificity, so the only
    questions left are order and importance, and both are facts rather than
    estimates. The cost is real and is the safe direction: a genuinely covering
    answer written any other way now reads as UNANSWERED. That is a false alarm,
    silenceable with a BENIGN row and a stated reason. The failure it removes was
    silence.

    IMPORTANCE, not specificity: the answer loads later, so equal importance
    already wins. It loses only to a vendor `!important`, which an important
    answer meets.

    What is NOT asked is the VALUE. A Genesis rule declaring the vendor's own
    value is still an answer, because the contract here is that a Genesis
    DECISION exists on this declaration — components.css deliberately restates
    `overflow: auto` on `.panel` with a comment saying why. Whether a given
    answer neutralises the leak it names is pinned next to that answer, in
    test_panel_layout_neutralisation.py.
    """
    return any(
        r["top"] and r["sel"] == leak["sel"] and (r["important"] or not leak["important"])
        for r in answer_rules
    )


def _resolve(href: str) -> Path | None:
    """Map a served href to its file under webui/, or None if we do not ship it.

    A query string or fragment is stripped first. `href="/css/x.css?v=3"` is an
    ordinary cache-buster and names a file we ship — and before this, it did not
    resolve, which was not a near-miss: the caller dropped every unresolvable
    href, found no owned sheet, and SKIPPED. One `?v=` on one link disarmed the
    whole guard for that page and reported `25 passed, 1 skipped`, exit 0.

    Returning None is now a FAILURE at the call site rather than a quiet drop.
    See `_unresolvable`.
    """
    path = href.split("#", 1)[0].split("?", 1)[0]
    candidate = WEBUI / path.lstrip("/")
    return candidate if candidate.is_file() else None


def _is_external(href: str) -> bool:
    """A link to something this repository does not ship at all.

    Absolute URLs and protocol-relative ones are somebody else's file. They are
    the one category that may be absent without the guard treating it as a
    defect — and they are named explicitly so that "we could not find it" and
    "it is not ours" stay different answers.
    """
    return bool(re.match(r"(?:[a-z][a-z0-9+.-]*:)?//", href, re.I))


def _unresolvable(hrefs: list[str]) -> list[str]:
    """Hrefs that should name a file under webui/ and do not."""
    return [h for h in hrefs if not _is_external(h) and _resolve(h) is None]


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
    # regex this replaced returned seven and said nothing. Pinned as the LIST,
    # not as a count against a string search of the source: that oracle was
    # itself a regex, and it went red — blaming the parser — when a link was
    # rewritten with single quotes, which is valid HTML and identical to a
    # browser. A guard whose own check is the pattern-matching it replaced can
    # only fail on the spellings its author happened to use.
    #
    # Compared with the query string and fragment stripped, because a
    # cache-buster is an ordinary edit and this test is about the PARSER, not
    # about the version of a file. Pinning the raw hrefs made `?v=3` on one link
    # fail here with a message about the parse.
    dash = [
        h.split("#", 1)[0].split("?", 1)[0]
        for h in _linked_stylesheets(TEMPLATE_DIR / "genesis_dashboard.html")
    ]
    assert dash == [
        "/index.css",
        "/css/tokens.css",
        "/css/components.css",
        "/css/modals.css",
        "/css/buttons.css",
        "/vendor/google/google-icons.css",
        "/vendor/ace-min/ace.min.css",
        "/css/dashboard.css",
    ], "the dashboard's stylesheet links changed — re-check the order, then this list"


@pytest.mark.parametrize("template", TEMPLATES, ids=lambda p: p.name)
def test_no_vendor_layout_rule_is_left_unanswered(template: Path):
    """Every vendor layout declaration on a shared selector has a later answer.

    Fails the way the `.panel` defect should have failed: naming the page, the
    selector and the property, before anyone has to look at the rendered page.
    """
    hrefs = _linked_stylesheets(template)
    missing = _unresolvable(hrefs)
    assert not missing, (
        f"{template.name} links stylesheets that name no file under webui/: "
        f"{missing}. Either the page is broken or this guard cannot read the "
        "sheet it is supposed to check — and it must not quietly stand down "
        "over either. A version of this test dropped unresolvable hrefs, found "
        "no owned sheet, and SKIPPED: one `?v=` cache-buster disarmed the whole "
        "page and the run reported `25 passed, 1 skipped`, exit 0."
    )
    order = {h: i for i, h in enumerate(hrefs)}
    owned = [h for h in hrefs if h.startswith(OWNED_PREFIX) and _resolve(h)]
    foreign = [h for h in hrefs if not h.startswith(OWNED_PREFIX) and _resolve(h)]
    if not owned or not foreign:
        # The LAST door into the skip, and the one the fix above did not close.
        # An href on another origin is excused from `_unresolvable` — correctly,
        # it is not ours to ship — and then fails `_resolve` too, so it lands in
        # neither list and the page skips. MEASURED: moving one page's three
        # links to a CDN gave `22 passed, 1 skipped`, exit 0, on a page with the
        # same leaks as before. Not ours to READ is not the same as clean.
        external = [h for h in hrefs if _is_external(h)]
        assert not external, (
            f"{template.name} links stylesheets on another origin ({external}). "
            "This guard cannot read them, so the page is UNCHECKED rather than "
            "clean, and a skip here is the same silent disarm already fixed at "
            "the resolver arriving by a different door. Vendor the sheet under "
            "webui/, or state the exemption here."
        )
        pytest.skip(f"{template.name} links no owned/vendor pair we ship")

    owned_decls = {h: _declarations(_resolve(h).read_text()) for h in owned}
    for href in foreign:
        assert _declarations(_resolve(href).read_text()), (
            f"{href} parsed to zero rules. A stylesheet that cannot be parsed "
            "reads as a sheet with no leaks, which is the wrong direction — "
            "check for an unclosed brace before believing this page is clean."
        )
    problems: list[str] = []

    for f_href in foreign:
        for sel_key, props in _declarations(_resolve(f_href).read_text()).items():
            for prop in sorted(set(props) & LAYOUT_PROPS):
                # Only a selector Genesis LAYS OUT can be leaked into. Two
                # narrowings, and the second is what keeps this usable:
                #
                #   * a selector Genesis never mentions is the vendor's business;
                #   * a selector Genesis styles WITHOUT any layout property is
                #     also the vendor's business. A base stylesheet setting
                #     `display` on `a` or `ul` is doing its job, and Genesis
                #     giving that element a colour is not a competing claim.
                #
                # Without the second, this reported nine findings across the
                # five pages, six of them element selectors where the only
                # overlap was that both sheets mention the tag.
                styling = [
                    h
                    for h in owned
                    if sel_key in owned_decls[h] and (set(owned_decls[h][sel_key]) & LAYOUT_PROPS)
                ]
                if not styling:
                    continue
                later = [h for h in styling if order[h] > order[f_href]]
                for leak in props[prop]:
                    # Per LEAK and per VALUE — a clearance is for a declaration.
                    if (sel_key, prop, leak["value"]) in BENIGN:
                        continue
                    if any(_answers(_rules(owned_decls[h], sel_key, prop), leak) for h in later):
                        continue
                    # Name WHY, because the four causes need different fixes.
                    same_selector = [
                        h
                        for h in later
                        if any(
                            r["sel"] == leak["sel"] for r in _rules(owned_decls[h], sel_key, prop)
                        )
                    ]
                    declared_later = [h for h in later if _rules(owned_decls[h], sel_key, prop)]
                    earlier = [
                        h
                        for h in styling
                        if order[h] < order[f_href] and _rules(owned_decls[h], sel_key, prop)
                    ]
                    if same_selector and leak["important"]:
                        why = (
                            f"answered on `{leak['sel']}` in {same_selector}, but the "
                            "vendor declaration is `!important` and the answer is not"
                        )
                    elif same_selector:
                        why = (
                            f"answered on `{leak['sel']}` in {same_selector}, but only "
                            "inside an at-rule — an answer under `@media` does not "
                            "answer at every width"
                        )
                    elif declared_later:
                        why = (
                            f"declared in {declared_later}, but on a different selector "
                            f"than `{leak['sel']}`. This guard matches selectors exactly: "
                            "write the answer on the vendor's own selector, or add a "
                            "BENIGN row saying why the different one is enough"
                        )
                    elif earlier:
                        why = f"styled in {earlier}, which loads EARLIER — the vendor rule wins"
                    else:
                        why = "no Genesis sheet declares it"
                    problems.append(f"{f_href} sets `{prop}` on `{sel_key}` — {why}")

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
    answer = _declarations((WEBUI / "css/components.css").read_text())

    for prop in ("display", "height"):
        leaks = _rules(vendor, ".panel", prop)
        assert leaks, (
            f"this test is about answering index.css's `.panel {{ {prop}: … }}`; "
            "if the vendor rule is gone the answer can go too, and so can this test"
        )
        # `_answers`, not "is the property present". An earlier version asked
        # `answer.get(prop, (False,))[0]`, which went VACUOUS the moment the
        # structure became a list of rules — the first RULE is truthy whatever it
        # says. Asking the real question cannot rot that way.
        for leak in leaks:
            assert _answers(_rules(answer, ".panel", prop), leak), (
                f"components.css must ANSWER index.css's `{prop}` on `{leak['sel']}` "
                "at the top level — a panel is a header ABOVE a body, and "
                "inheriting AZ's flex lays the two out side by side"
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
        for sel_key, props in _declarations(sheet.read_text()).items():
            for prop, rules in props.items():
                if prop in LAYOUT_PROPS and any(r["top"] for r in rules):
                    answered_somewhere.add((sel_key, prop))

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
            for sel_key, props in _declarations(_resolve(f_href).read_text()).items():
                for prop in sorted(set(props) & LAYOUT_PROPS):
                    if (sel_key, prop) not in answered_somewhere:
                        continue
                    later = [h for h in owned if order[h] > order[f_href]]
                    if all(
                        (sel_key, prop, leak["value"]) in BENIGN
                        or any(_answers(_rules(owned_decls[h], sel_key, prop), leak) for h in later)
                        for leak in props[prop]
                    ):
                        continue
                    gaps.append(
                        f"{template.name}: {f_href} sets `{prop}` on `{sel_key}`, which "
                        "Genesis answers on other pages but not on this one"
                    )

    assert not gaps, (
        "a vendor layout rule is neutralised on some pages and not others:\n  "
        + "\n  ".join(sorted(set(gaps)))
        + "\n\nMove the answer to a stylesheet every affected page loads "
        "(css/components.css) rather than answering it per page."
    )


def _leak(css: str, key: str, prop: str) -> dict:
    """The single rule a one-rule stylesheet declares, for a comparison test."""
    rules = _rules(_declarations(css), key, prop)
    assert len(rules) == 1, f"expected one rule, got {len(rules)}"
    return rules[0]


def _answered(answer_css: str, vendor_css: str, key: str, prop: str) -> bool:
    """Does the answer sheet answer the vendor sheet's single rule on `key`?"""
    return _answers(_rules(_declarations(answer_css), key, prop), _leak(vendor_css, key, prop))


# Answers that are NOT the vendor's selector. Every one of these read as an
# ANSWER under the coverage model this replaced, because it compared flat token
# SETS and a set cannot see position or multiplicity. The first row is the one
# that mattered: substituting `.panel .panel` for the shipped answer put the
# original incident back with 26 tests green.
@pytest.mark.parametrize(
    "answer_selector,why",
    [
        (".panel .panel", "a panel inside another panel, which is not a panel"),
        (".panel > .panel", "same elements as above, one combinator narrower"),
        (".panel + .panel", "a panel that FOLLOWS a panel"),
        ("#chat.panel", "a panel that is also #chat — matches zero elements here"),
        (".panel:hover", "only while someone is pointing at it"),
        (".wrap .panel", "only panels under .wrap"),
        (".panel.wide", "only panels that also carry .wide"),
    ],
)
def test_an_answer_on_a_different_selector_does_not_answer(answer_selector, why):
    assert not _answered(
        answer_selector + " { display: block }",
        ".panel { display: flex }",
        ".panel",
        "display",
    ), f"`{answer_selector}` was accepted as an answer to `.panel` — {why}"


def test_the_vendors_own_selector_does_answer():
    """The other direction, so the test above cannot pass by refusing everything.

    A guard that rejects every answer is as useless as one that accepts every
    answer, and it is the easier of the two to ship by accident.
    """
    assert _answered(".panel { display: block }", ".panel { display: flex }", ".panel", "display")


def test_a_selector_GROUP_answers_through_the_member_that_matches():
    """`#app .panel, .panel` answers a vendor `.panel`, through its second member.

    Recorded per group MEMBER rather than per rule, so a group is neither judged
    on its strongest member (which would accept an answer that does not apply
    everywhere) nor on its weakest (which would reject this).
    """
    assert _answered(
        "#app .panel, .panel { display: block }",
        ".panel { display: flex }",
        ".panel",
        "display",
    )
    # And the same group does NOT answer a vendor rule on `#app .panel` through
    # its plain `.panel` member being present — identity is per member.
    assert _answered(
        "#app .panel, .panel { display: block }",
        "#app .panel { display: flex }",
        ".panel",
        "display",
    )
    assert not _answered(
        ".panel { display: block }",
        "#app .panel { display: flex }",
        ".panel",
        "display",
    )


def test_selector_text_is_compared_normalised_not_byte_for_byte():
    """Whitespace and group ORDER must not decide whether a leak is answered.

    `body, html` and `html, body` are the same selector list, and the vendor
    writes the page root one way while Genesis writes it the other. Comparing
    raw text would have made that a finding on every page.
    """
    assert _answered(
        "html,\n  body { overflow: auto }",
        "body,html{overflow:hidden}",
        "html",
        "overflow",
    )


def test_an_answer_scoped_to_an_AT_RULE_does_not_answer_at_every_width():
    """A vendor rule applies at all widths; an answer under `@media` does not."""
    assert not _answered(
        "@media (min-width: 900px) { .panel { display: block } }",
        ".panel { display: flex }",
        ".panel",
        "display",
    )


def test_an_important_vendor_declaration_needs_an_important_answer():
    """Loading later is enough at equal importance, and not enough below it."""
    vendor = ".panel { display: flex !important }"
    assert not _answered(".panel { display: block }", vendor, ".panel", "display")
    assert _answered(".panel { display: block !important }", vendor, ".panel", "display")
    # And an important ANSWER is never required when the vendor is not important.
    assert _answered(".panel { display: block }", ".panel { display: flex }", ".panel", "display")


def test_a_query_string_on_an_href_does_not_disarm_the_guard():
    """The silent-disarm blocker, at the resolver.

    `_resolve` returning None used to drop the href; the caller then found no
    owned sheet and SKIPPED the page. A cache-buster on one link therefore
    turned the whole check off and reported `25 passed, 1 skipped`, exit 0.
    """
    assert _resolve("/css/components.css?v=3") == WEBUI / "css/components.css"
    assert _resolve("/css/components.css#top") == WEBUI / "css/components.css"
    assert _unresolvable(["/css/components.css?v=3"]) == []


def test_an_href_we_cannot_resolve_is_reported_rather_than_skipped():
    """The same blocker at the call site: unknown means FAIL, not stand down.

    An external URL is the one absence that is not a defect, and it is named
    explicitly so "we could not find it" and "it is not ours" stay different
    answers.
    """
    assert _unresolvable(["/css/no-such-sheet.css"]) == ["/css/no-such-sheet.css"]
    assert _unresolvable(["https://fonts.example/x.css", "//cdn.example/y.css"]) == []


def test_the_real_pages_resolve_every_stylesheet_they_link():
    """Guard the guard: the test above proves the helper, this proves the corpus.

    If a page starts linking something this repository does not ship, the
    per-page test fails loudly rather than skipping — and this says so first,
    once, instead of once per page.
    """
    unresolved = {
        t.name: _unresolvable(_linked_stylesheets(t))
        for t in TEMPLATES
        if _unresolvable(_linked_stylesheets(t))
    }
    assert not unresolved, f"stylesheet links naming no file under webui/: {unresolved}"


# ---------------------------------------------------------------------------
# Round five. Every test below pins a hole an adversarial audit MUTATED AND RAN
# against the version before it, with the observed result in the docstring.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "vendor,answer",
    [
        ("BODY { display: flex }", "body { display: block }"),
        ("body { display: flex }", "BODY { display: block }"),
        ("BODY, HTML { display: flex }", "html, body { display: block }"),
        ("DIV.panel { display: flex }", "div.panel { display: block }"),
    ],
)
def test_a_type_selector_is_matched_case_INSENSITIVELY(vendor, answer):
    """Type names are ASCII case-insensitive in HTML (Selectors 4 §6.1).

    MEASURED before the fold existed: rewriting the vendor sheet's own
    `body,\\nhtml {` as `BODY,\\nHTML {` left the whole `overflow: hidden;
    position: fixed; height: 100%` viewport lock in place with all 23 tests
    green — the second of the two incidents this file exists for, reproduced
    with the shift key. The control case, the same rule in lowercase, failed on
    all five pages.
    """
    key = next(iter(_selector_keys(tinycss2.parse_component_value_list(vendor.split("{")[0]))))
    assert _answered(answer, vendor, key, "display")


def test_class_and_id_names_are_NOT_folded():
    """The other half, which a blanket `.lower()` would have got wrong.

    Class and id names ARE case-sensitive, so `.Panel` and `.panel` are two
    different selectors and an answer on one does not answer the other.
    """
    assert not _answered(
        ".panel { display: block }", ".Panel { display: flex }", ".Panel", "display"
    )


@pytest.mark.parametrize(
    "vendor,answer",
    [
        (".wrap>.panel { display: flex }", ".wrap > .panel { display: block }"),
        (".wrap  >  .panel { display: flex }", ".wrap>.panel { display: block }"),
        ("body   .panel { display: flex }", "body .panel { display: block }"),
    ],
)
def test_whitespace_around_a_combinator_is_insignificant(vendor, answer):
    """A minified vendor sheet writes `a>b`; any hand-written answer writes `a > b`.

    The module docstring claimed selector text was normalised for whitespace
    while only the ends were stripped, so these compared unequal. The direction
    was fail-CLOSED — a false alarm, not a leak — but this file's whole trade is
    accepting false alarms, so manufacturing them is not free.
    """
    assert _answered(answer, vendor, ".panel", "display")


def test_a_BENIGN_row_clears_its_own_VALUE_and_not_the_slot():
    """The clearance is for a declaration, never for a (selector, property) pair.

    MEASURED against the version before this: changing the vendor's
    `width: 100%` on the page root to `width: 250px` left all 23 tests green — a
    250px-wide document on all five pages, cleared by a row written for a
    different number, while the comment beside that row said in as many words
    that 250px "is a real leak and must still fail".
    """
    assert ("html", "width", "100%") in BENIGN
    assert ("html", "width", "250px") not in BENIGN

    vendor = _declarations("html { width: 250px }")
    leak = _rules(vendor, "html", "width")[0]
    assert leak["value"] == "250px"
    assert (("html", "width", leak["value"]) in BENIGN) is False


def test_every_BENIGN_row_still_matches_a_real_vendor_declaration():
    """Guard the guard: a row nothing exercises is a reason nobody re-reads.

    An audit called one of these rows dead. It was — under the narrower property
    set this file carried at the time — and widening the set made it live again,
    which is exactly why the claim needed re-measuring rather than acting on.
    """
    vendor = _declarations((WEBUI / "index.css").read_text())
    for sel_key, prop, value in BENIGN:
        rules = _rules(vendor, sel_key, prop)
        assert any(r["value"] == value for r in rules), (
            f"BENIGN clears `{prop}: {value}` on `{sel_key}`, which index.css no "
            "longer declares — delete the row rather than leaving a clearance "
            "for a declaration that is gone"
        )


def test_a_NESTED_rule_is_refused_rather_than_dropped():
    """Native CSS nesting is invisible to a declarations-only reader.

    MEASURED: wrapping the vendor's `.panel` rule inside `#app-root { … }` left
    all 23 tests green while the browser still applied it. The guard now raises
    instead, which is the loud direction — a shape it cannot read is not a pass.
    """
    with pytest.raises(AssertionError, match="NESTED rule"):
        _declarations("#app-root { color: red; .panel { display: flex } }")


def test_an_unrecognised_block_at_rule_is_refused_rather_than_dropped():
    """The at-rule walk is an allowlist, and an allowlist has a tail.

    `@scope` wraps ordinary rules and was silently dropped, so every rule inside
    it read as absent. Descending needs prelude resolution; refusing does not,
    and refusing is the direction that cannot go quiet.
    """
    with pytest.raises(AssertionError, match="does not descend"):
        _declarations("@scope (.a) { .panel { display: flex } }")
    # And the ones that genuinely cannot target elements stay silent.
    assert _declarations("@keyframes spin { from { opacity: 0 } to { opacity: 1 } }") == {}
    assert _declarations("@font-face { font-family: x; src: url(x.woff2) }") == {}


def test_every_real_page_is_CHECKED_and_not_merely_unskipped():
    """Guard the guard, and the last door into the skip.

    `_is_external` excuses another origin's sheet from `_unresolvable` — rightly,
    it is not ours to ship — and it also fails `_resolve`, so before this it fell
    into neither list and the page skipped. MEASURED: moving one page's three
    links to a CDN gave `22 passed, 1 skipped`, exit 0, on a page whose leaks had
    not changed. `test_the_template_scan_found_the_pages` could not see it: it
    counts TEMPLATES, which was still five.
    """
    unchecked = []
    for template in TEMPLATES:
        hrefs = _linked_stylesheets(template)
        owned = [h for h in hrefs if h.startswith(OWNED_PREFIX) and _resolve(h)]
        foreign = [h for h in hrefs if not h.startswith(OWNED_PREFIX) and _resolve(h)]
        if not owned or not foreign:
            unchecked.append(f"{template.name} (owned={len(owned)}, vendor={len(foreign)})")
    assert not unchecked, (
        "these pages link no readable owned/vendor pair, so the per-page test "
        f"skips them and they are UNCHECKED rather than clean: {unchecked}"
    )


def test_the_layout_property_set_covers_the_near_neighbours_of_both_incidents():
    """`height` without `max-height` is a set that lets its own class through.

    MEASURED: `max-height: 120px` on the vendor's `.panel` rule clipped every
    panel on every page to 120px with all 23 tests green, because the property
    was not in the set and `height: auto` does not answer it.
    """
    for prop in ("height", "min-height", "max-height", "overflow", "overflow-x", "overflow-y"):
        assert prop in LAYOUT_PROPS
