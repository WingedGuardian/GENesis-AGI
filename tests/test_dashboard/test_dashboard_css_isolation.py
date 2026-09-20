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

SCOPE, stated exactly, because every previous version of this paragraph claimed
more than the code did. What is checked is: a vendor layout declaration is
answered when a Genesis rule declares the same property, **on the same selector
text**, at the top level, in a sheet that loads later, carrying ``!important`` if
the vendor declaration does.

"The same selector text" is the whole boundary and it is narrower than it sounds.
The guard relates a leak to an answer only when both sheets write the selector
identically — after normalising whitespace, selector-group order and the case of
type names, but not otherwise. A vendor rule that reaches the same elements
through a different spelling is NOT related to a Genesis answer, and is skipped
rather than reported.

That is a deliberate retreat, not an oversight. Deciding which selectors reach
which elements is what the two previous designs did, and both failed open in
shapes nobody predicted — four rounds of it. An estimate that runs before the
comparison decides whether a rule is ever looked at, so its errors are silent by
construction. There is now no estimate anywhere in the pipeline; the price is
this narrower reach, and the price is paid knowingly.

What the guard REFUSES rather than skipping. Silence reads as a clear, so a
shape this guard cannot read must stop it rather than slip past. Each of these
is pinned by a test that constructs it, because a refusal list is exactly the
kind of prose a reader trusts INSTEAD of re-checking — an earlier version of
this paragraph named four refusals the code did not perform, which is worse than
naming none:

* a rule nested inside another rule, and a conditional nested inside one;
* an at-rule it does not descend into, block or blockless, including
  ``@namespace`` — prefixes are per-sheet, so identical selector text in two
  sheets can target different elements and identity cannot tell;
* ``@layer`` carrying a layout declaration, whose precedence outranks both the
  things compared here;
* an ``@import`` naming any host but the font services listed in
  ``_IMPORT_HOSTS``, since its contents are never read;
* a stylesheet link on another origin — checked BEFORE the owned and vendor
  lists are built, so a page that still has a local pair cannot sidestep it;
* any sheet the browser does not apply unconditionally: ``media`` on a ``<link>``
  OR on a ``<style>``, ``disabled``, and ``rel="alternate stylesheet"``. One
  function answers that question for both element kinds;
* a stylesheet that parses to zero rules, or whose selector could not be parsed —
  a stray brace is not reported as an error, it is folded into the NEXT rule's
  prelude, so the rule after it keys under the wrong selector;
* a page emitter this guard does not know about. The page population is
  DERIVED — every module under the dashboard and hosting trees that contains a
  stylesheet link — and each must be mapped to the page it serves or exempted
  with a reason. Four defects have shipped through a page that no enumeration
  happened to name;
* a vendor layout rule whose selector embeds another selector, like
  ``:is(.panel)``, WHERE the embedded text names something Genesis lays out. A
  reader expects a ``.panel`` answer to count there and identity cannot say so.
  Scoped deliberately: the unscoped version fired on a vendor pseudo naming
  nothing this repository styles, and a refusal that is mostly noise gets
  deleted.

What it still does NOT model, deliberately: the cascade. Shorthands are not
expanded (``flex`` is not read as implying ``flex-direction``), and specificity is
never computed — identity makes it unnecessary, since two rules with the same
selector have the same specificity by definition.

It does not check the VALUE either. A Genesis rule restating the vendor's own
value is still an answer, because what this file asserts is that a Genesis
DECISION exists on that declaration — ``components.css`` deliberately restates
``overflow: auto`` on ``.panel`` with a comment saying why. Whether a given answer
neutralises the leak it names is pinned beside that answer, in
``test_panel_layout_neutralisation.py``.

One gap remains and is not refusable. The per-page test only fires where Genesis
has ALREADY staked a layout claim on the identical selector; a vendor rule on a
selector no Genesis sheet lays out is the vendor's business, and without that
narrowing the check drowns in ``a``, ``ul`` and ``input``. The cross-page test
covers part of it — it fires when an answer exists SOMEWHERE and is missing on
one page, which is what the second incident was — but a leak nobody has ever
answered on any page is outside both. Neither of these tests found the third
incident; a person looking at a page did, and that remains true.
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path

import pytest
import tinycss2

from tests.test_dashboard.dashboard_pages import stylesheet_pages

ROOT = Path(__file__).resolve().parents[2]
WEBUI = ROOT / "src/genesis/dashboard/webui"
TEMPLATE_DIR = ROOT / "src/genesis/dashboard/templates"

# Genesis owns /css/* (the resolved path's parent — see `_owned_sheet_text`).
# Everything else a page links is inherited or vendored.

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
        "flex",
        "flex-flow",
        "inset",
        "grid-template",
        "grid-area",
        "aspect-ratio",
        "contain",
    }
)

# At-rules whose contents cannot target elements, so not descending into them
# loses nothing. Anything NOT here and not descended into raises, because the
# alternative is a whole block of rules reading as absent. `@keyframes` wraps
# step rules whose "selectors" are `from`/`to`/percentages; the rest wrap
# descriptors or nothing at all.
# Hosts whose imported stylesheets this guard does not follow, with the reason.
# `index.css:1` opens with a font-service import; a blanket refusal of blockless
# at-rules — as one review suggested — fails the shipped file immediately, and a
# blanket EXEMPTION of external imports drops a CDN layout sheet whole.
_IMPORT_HOSTS = frozenset({"fonts.googleapis.com", "fonts.gstatic.com"})

# Blockless at-rules that can neither introduce a rule nor hide one.
_BLOCKLESS_AT_RULES_WITHOUT_RULES = frozenset({"charset"})

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
    (".collapse:not(.show)", "display", "none"): (
        "Bootstrap's collapse behaviour, and the ONE instance in the vendor sheet "
        "of a selector that embeds another. It is excused rather than refused "
        "because neither class it names is one Genesis lays out: no Genesis sheet "
        "declares `.collapse` at all, and the only `.show` rule is `.modal.show` "
        "in modals.css, a different selector. `display: none` on an element the "
        "markup has explicitly collapsed is that element doing what it is told."
    ),
    (".section", "width", "100%"): (
        "`width: 100%` on a block box, and border-box means the padding and "
        "border modals.css adds sit INSIDE the 100% rather than overflowing it."
    ),
}


_APPLIES_UNCONDITIONALLY = frozenset({"", "all", "screen"})


def _not_applied(attrs: dict[str, str], rel: list[str]) -> str | None:
    """Why this stylesheet is NOT unconditionally applied, or None if it is.

    ONE function for a question that was being answered in four places from four
    partial rules, and not at all in three more. Each place was a place to forget
    an attribute, and each forgotten attribute is a sheet whose declarations this
    guard counts as live when the browser does not — or the reverse.

    MEASURED, each as a live fail-open on the real pages before this existed:
    marking the sheet that carries the `.panel` answer `rel="alternate
    stylesheet"` left 46 tests green, and so did marking it `disabled`; a
    `<style media="print">` was recorded as an unconditional answer.

    An unrecognised `media` value RAISES rather than guessing. `print` and
    `(min-width: 900px)` are both "not unconditional", but so is anything else,
    and a guard that quietly treats an unknown condition as "applies" is the
    shape this whole file exists to stop.
    """
    if "alternate" in rel:
        return "rel=alternate stylesheet — not applied unless the user picks it"
    if "disabled" in attrs:
        return "disabled"
    mime = attrs.get("type", "").strip().lower()
    if mime and mime not in {"text/css", "stylesheet", "text/css;charset=utf-8"}:
        # `<style type="text/plain">` is a data block, not a sheet — the browser
        # never parses it, so its declarations cannot answer anything.
        return f"type={mime}"
    media = attrs.get("media", "").strip().lower()
    if media not in _APPLIES_UNCONDITIONALLY:
        return f"media={media}"
    return None


class _LinkCollector(HTMLParser):
    """Every stylesheet a page carries, in document order.

    LINKS AND INLINE `<style>` BLOCKS BOTH, because the cascade does not care
    which one a declaration arrived in and neither can this. An earlier version
    collected links only and said so in its docstring — "inline `<style>` blocks
    in a template are invisible" — which is a gap, not a scope: MEASURED, four of
    the six pages declare layout properties inline, 47 selectors' worth on one of
    them. A Genesis layout claim the guard cannot see makes the vendor
    declaration it answers read as unclaimed, and the page is skipped rather than
    checked.

    Each entry is `("link", href)` or `("inline", css)`; the list index is the
    load order, which is what `_answers` needs and the only thing position is
    used for.

    A MEDIA-CONDITIONAL link is refused rather than recorded. `media="print"`
    means the sheet applies under that condition only, so treating its
    declarations as universal answers a leak that is still live at every other
    width. There are none today.

    The regex this class replaced required `rel` before `href` and so missed
    `<link href="…" rel="stylesheet">` — one of the dashboard's eight links,
    silently, while its docstring claimed to return them all.
    """

    def __init__(self) -> None:
        super().__init__()
        self.sheets: list[tuple[str, str]] = []
        self.not_applied: list[str] = []
        self._in_style = False
        self._style_attrs: dict[str, str] = {}
        # Depth inside inert containers. Content inside <template> is parsed
        # into a document fragment and applies NOTHING until cloned into the
        # document — a <style> or <link> there can never answer a leak.
        self._inert_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag == "template":
            self._inert_depth += 1
            return
        if tag == "style":
            self._in_style = True
            self._style_attrs = a
            if self._inert_depth:
                self.not_applied.append("<style> (inside <template> — inert)")
            return
        if tag != "link":
            return
        rel = a.get("rel", "").lower().split()
        if "stylesheet" not in rel or not a.get("href"):
            return
        if self._inert_depth:
            self.not_applied.append(f"{a['href']} (inside <template> — inert)")
            return
        why = _not_applied(a, rel)
        if why:
            self.not_applied.append(f"{a['href']} ({why})")
            return
        self.sheets.append(("link", a["href"]))

    def handle_endtag(self, tag: str) -> None:
        if tag == "style":
            self._in_style = False
        elif tag == "template" and self._inert_depth:
            self._inert_depth -= 1

    def handle_data(self, data: str) -> None:
        if not (self._in_style and data.strip()):
            return
        if self._inert_depth:
            return  # already named as inert at the start tag
        # THE SAME QUESTION, asked of a `<style>` as of a `<link>`. An earlier
        # version asked it of links only, so `<style media="print">` was recorded
        # as an unconditional answer while the browser applied it nowhere on
        # screen. One function, both element kinds — the four partial answers
        # this replaces were four places to forget an attribute.
        why = _not_applied(self._style_attrs, ["stylesheet"])
        if why:
            self.not_applied.append(f"<style> ({why})")
            return
        self.sheets.append(("inline", data))


def _sheets(html: str) -> tuple[list[tuple[str, str]], list[str]]:
    """(sheets in load order, sheets refused as not-unconditionally-applied).

    Takes TEXT, because one of the six pages has no path — the login page is a
    Python string. See `dashboard_pages.stylesheet_pages`.
    """
    parser = _LinkCollector()
    parser.feed(html)
    return parser.sheets, parser.not_applied


def _linked_stylesheets(html: str) -> list[str]:
    """Just the hrefs, for the checks that are about LINKS specifically."""
    return [value for kind, value in _sheets(html)[0] if kind == "link"]


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
            # Collapse around a COMBINATOR TOKEN, here, rather than by rewriting
            # the finished string. The regex that did the latter reached inside
            # quoted values too, so `.panel[data-slot="a > b"]` and
            # `.panel[data-slot="a>b"]` normalised to the same text and answered
            # each other — a false clear, introduced by the fix for a false
            # clear. Token types cannot be fooled by their own contents.
            if _is_combinator(tokens[i - 1] if i else None) or _is_combinator(
                next((t for t in tokens[i + 1 :] if t.type != "whitespace"), None)
            ):
                continue
            out.append(" ")
            continue
        text = tinycss2.serialize([token])
        # An ident is a TYPE name only when nothing binds it to a class, id or
        # pseudo. `.Panel` and `#App` keep their case; `DIV` does not — provided
        # it names an HTML element. ASCII case-insensitivity is a property of the
        # HTML namespace ONLY: SVG/MathML element names are case-sensitive
        # (`linearGradient` ≠ `lineargradient`), so folding an ident that is not
        # a known HTML element name would collide two different foreign elements
        # (Codex P2, #2038). Unknown mixed-case idents keep their case instead.
        prev = tinycss2.serialize([tokens[i - 1]]) if i else ""
        if (
            token.type == "ident"
            and prev not in {".", "#", ":", "::"}
            and (token.lower_value in _HTML_ELEMENTS or token.value.islower())
        ):
            text = text.lower()
        out.append(text)
    return "".join(out).strip()


def _is_combinator(token) -> bool:
    """A child/sibling combinator as a TOKEN, never as a character in some value."""
    return token is not None and token.type == "literal" and token.value in {">", "+", "~"}


# Type selectors that ASCII-fold in the HTML namespace. Everything else —
# `linearGradient`, `feGaussianBlur`, `annotation` — is foreign-namespace and
# case-SENSITIVE, so it is left alone rather than folded into a different name.
_HTML_ELEMENTS = frozenset(
    [
        "a", "abbr", "address", "area", "article", "aside", "audio", "b", "base",
        "bdi", "bdo", "blockquote", "body", "br", "button", "canvas", "caption",
        "cite", "code", "col", "colgroup", "data", "datalist", "dd", "del",
        "details", "dfn", "dialog", "div", "dl", "dt", "em", "embed",
        "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
        "h4", "h5", "h6", "head", "header", "hgroup", "hr", "html", "i",
        "iframe", "img", "input", "ins", "kbd", "label", "legend", "li", "link",
        "main", "map", "mark", "math", "menu", "meta", "meter", "nav",
        "noscript", "object", "ol", "optgroup", "option", "output", "p",
        "picture", "pre", "progress", "q", "rp", "rt", "ruby", "s", "samp",
        "script", "section", "select", "slot", "small", "source", "span",
        "strong", "style", "sub", "summary", "sup", "svg", "table", "tbody",
        "td", "template", "textarea", "tfoot", "th", "thead", "time", "title",
        "tr", "track", "u", "ul", "var", "video", "wbr",
    ]
)


# ALLOWLIST, not a denylist of six names. The six-name version missed `:host()`,
# `:host-context()`, `::slotted()` and `:nth-child(1 of .panel)` — all measured
# shipping a live `.panel` leak. Polarity flipped for the same reason it was
# flipped for at-rules and for the guard as a whole: a construct invented next
# year must fail loudly rather than vanish.
_TAKES_NO_SELECTOR = frozenset(
    {"lang", "dir", "nth-child", "nth-last-child", "nth-of-type", "nth-last-of-type"}
)


def _functional_pseudo_args(selector: str) -> list[tuple[str, list]]:
    """(lower name, argument tokens) for every functional pseudo in the text.

    A token walk, because the regex this replaced counted parens in SERIALISED
    text — and a quoted paren inside an attribute value
    (`:is(.panel[data-slot="("])`) is not a delimiter, so the pattern desynced
    on it and matched the tail as a selector (Codex P2, #2038). tinycss2 has
    already resolved the nesting; a function token's `arguments` end where the
    parser says they end, never where a character count does.
    """
    out: list[tuple[str, list]] = []
    tokens = tinycss2.parse_component_value_list(selector)
    for i, tok in enumerate(tokens):
        if tok.type != "function":
            continue
        j = i - 1
        while j >= 0 and tokens[j].type == "whitespace":
            j -= 1
        if j < 0 or tokens[j].type != "literal" or tokens[j].value not in {":", "::"}:
            continue  # a plain function such as `var(…)`, not a pseudo
        out.append((tok.lower_name, tok.arguments))
    return out


def _embedded_selectors(selector: str) -> list[str]:
    """The selector text a functional pseudo-class carries inside it.

    The one construct identity cannot be honest about. `:is(.panel)` reaches
    exactly the elements `.panel` reaches, so a reader seeing a Genesis `.panel`
    answer beside a vendor `:is(.panel)` leak expects the guard to connect them,
    and under identity it does not, because the two texts differ.

    MEASURED: appending `:is(.panel) { display: flex; height: 100% }` to the real
    vendor sheet shipped silently with 37 passed, `.panel` answered in
    components.css all the while. Relating the two means deciding which selectors
    reach which elements, which is the model this file deleted after four rounds
    of it failing open. So the shape is refused — but only where the embedded
    text names something Genesis actually lays out, because a vendor sheet is
    full of pseudos that name nothing we touch and refusing those is noise.

    `:nth-child(2n+1)` and friends take a formula, not a selector — except in the
    `of S` form, which does. Anything not on the takes-no-selector list is
    treated as carrying one.
    """
    out: list[str] = []
    for name, arg_tokens in _functional_pseudo_args(selector):
        if name in _TAKES_NO_SELECTOR:
            # `of S` — S is the selector list AFTER the `of` ident token, not a
            # substring of the formula; `2n+ofx` cannot fake one.
            plain = [t for t in arg_tokens if t.type != "whitespace"]
            for k, t in enumerate(plain):
                if t.type == "ident" and t.lower_value == "of":
                    arg_tokens = plain[k + 1 :]
                    break
            else:
                continue
        args = tinycss2.serialize(arg_tokens).strip()
        if args:
            out.append(args)
        # Nested pseudos (`:is(:not(.x))`) carry their own selectors.
        out.extend(_embedded_selectors(args))
    return out


def _selector_keys(prelude: list) -> set[str]:
    """The selector list, as its normalised members. The key IS the selector.

    COLLAPSED from an approximation. The previous version reduced each member to
    a "key" — last compound, classes only, bare elements, parenthesised arguments
    stripped — so that two DIFFERENT selectors could be recognised as reaching the
    same elements. That was the last estimate left in the pipeline, it ran BEFORE
    the exact comparison, and it decided whether a rule was seen at all.

    MEASURED on the shipped vendor sheet: 30 of its 175 layout declarations (17%)
    derived NO key and were invisible to the guard entirely, `#right-panel
    { display: flex }` among them. A rule nothing can see is a rule nothing can
    report, which is the silent direction.

    Since `_answers` already compares normalised selector text, the key can BE
    that text: one stage instead of two, and no approximation anywhere. It costs
    nothing measurable — zero new findings on the real corpus, because the
    interest narrowing still requires Genesis to lay out that same selector — and
    it deletes the last-compound split, the ASCII class regex, the bare-element
    rule and `_strip_parens` along with the whole class of finding they generated.
    """
    return set(_selector_groups(prelude))


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
                # BOTH nested forms. An earlier version checked only for a nested
                # QUALIFIED rule, so `.shell { @media (min-width: 1px) { .panel
                # { display: flex } } }` slipped through the refusal that exists
                # for exactly this — the outer rule has no direct declarations,
                # so it was skipped for being empty while the browser applied the
                # rule two levels down.
                nested = [n for n in contents if n.type in {"qualified-rule", "at-rule"}]
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
                # A selector list cannot contain a brace. tinycss2 does not
                # report a stray `}` as an error — it RECOVERS by folding the
                # junk into the next rule's prelude, so the rule after it keys
                # as `} .panel` instead of `.panel`. MEASURED: one stray brace
                # before a leak shipped it with every test green.
                # The token TYPE, verified rather than assumed: a stray `}` does
                # not arrive as a literal whose value is "}". tinycss2 hands it
                # back as an ERROR token inside the prelude, and a check written
                # for the literal read as clean while the leak shipped.
                junk = [
                    t
                    for t in node.prelude
                    if t.type == "error" or (t.type == "literal" and t.value in {"{", "}", ";"})
                ]
                assert not junk, (
                    f"the selector `{tinycss2.serialize(node.prelude).strip()[:60]}` "
                    "could not be parsed as a selector, which means the stylesheet "
                    "above it does not parse and tinycss2 folded the junk into "
                    "this prelude. The rule is keyed under the wrong selector and "
                    "every leak in it reads as absent — fix the syntax error above."
                )
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
                if node.lower_at_keyword in {"media", "supports", "container"}:
                    walk(tinycss2.parse_rule_list(node.content), top_level=False)
                elif node.lower_at_keyword == "layer":
                    # `@layer` is NOT a conditional. Layer order beats everything
                    # this guard models: an important declaration in a layer
                    # outranks an important unlayered answer however the sheets
                    # load, so flattening it would report a leak answered by a
                    # rule the browser discards. Modelling it means modelling
                    # layer ORDER, which is the kind of estimate this file has
                    # now deleted twice. Refused instead.
                    layered = _declarations(tinycss2.serialize(node.content))
                    if any(prop in LAYOUT_PROPS for props in layered.values() for prop in props):
                        raise AssertionError(
                            "`@layer` carries a layout declaration. Layer precedence "
                            "outranks load order and importance, which is what this "
                            "guard compares, so it cannot judge the rules inside. "
                            "Move the declaration out of the layer, or teach this "
                            "file layer ORDER rather than flattening it."
                        )
                elif node.lower_at_keyword not in _AT_RULES_WITHOUT_ELEMENTS:
                    raise AssertionError(
                        f"`@{node.lower_at_keyword}` wraps rules this guard does not "
                        "descend into, so every rule inside it is invisible — which "
                        "is a clear, not a pass. Add it to the descend list if it "
                        "wraps ordinary rules, or to _AT_RULES_WITHOUT_ELEMENTS with "
                        "the reason its contents cannot target elements."
                    )
            elif node.type == "at-rule" and node.content is None:
                # BLOCKLESS at-rules, which the branch above never sees because
                # it tests `content is not None`.
                keyword = node.lower_at_keyword
                target = tinycss2.serialize(node.prelude).strip()
                if keyword == "import":
                    # A HOST allowlist, not "any absolute URL". The earlier
                    # version exempted every external import, so a vendor sheet
                    # importing `https://cdn.example/layout.css` was dropped
                    # whole — MEASURED: 46 tests green with the import in place.
                    host = re.search(r"//([^/\"')]+)", target)
                    if not host or host.group(1).lower() not in _IMPORT_HOSTS:
                        raise AssertionError(
                            f"`@import {target}` names a stylesheet this guard does "
                            "not follow, so every rule in it is invisible. Link it "
                            "from the page, where it is read like any other sheet, "
                            "or add its host to _IMPORT_HOSTS with the reason its "
                            "content cannot carry a layout rule."
                        )
                elif keyword == "namespace":
                    # Namespace prefixes are per-sheet, so `x|a` in two sheets can
                    # target different elements while comparing as identical text.
                    # Identity is unsound across a namespace mapping.
                    raise AssertionError(
                        f"`@namespace {target}` maps a prefix that is local to this "
                        "stylesheet, so identical selector text in two sheets can "
                        "target different elements and identity cannot tell. Remove "
                        "the mapping, or teach the key its resolved namespace URI."
                    )
                elif keyword not in _BLOCKLESS_AT_RULES_WITHOUT_RULES:
                    raise AssertionError(
                        f"`@{keyword}` is a blockless at-rule this guard does not "
                        "understand. It is skipped today, which is a clear rather "
                        "than a pass — add it to _BLOCKLESS_AT_RULES_WITHOUT_RULES "
                        "with the reason it cannot introduce or hide a rule."
                    )

    nodes = tinycss2.parse_stylesheet(css, skip_whitespace=True, skip_comments=True)
    errors = [n for n in nodes if n.type == "error"]
    assert not errors, (
        f"this stylesheet does not parse: {errors[0].message}. tinycss2 recovers by "
        "folding the junk into the NEXT rule's prelude, so a stray brace re-keys "
        "the rule after it — MEASURED: one `}` before a `.panel` leak re-keyed it "
        "to `} .panel` and the leak shipped with every test green. A sheet that "
        "does not parse is not a sheet with no leaks."
    )
    walk(nodes, True)
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
    # NORMALISED, because the caller classifies ownership from the parent
    # directory. `/css/../index.css` is a link the browser loads as the VENDOR
    # sheet; left unnormalised its parent is still `css/`, so it read as ours.
    candidate = (WEBUI / path.lstrip("/")).resolve()
    if WEBUI.resolve() not in candidate.parents:
        return None  # escapes the served tree: not a file we ship
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


def _owned_sheet_text(href: str) -> str:
    """The sheet's text when the RESOLVED path is an owned one, else "".

    ONE ownership test for every collector: the resolved file's parent must be
    webui/css — the same rule `_PageSheets` applies. The cross-page collector
    used to classify from the href's literal prefix, so `/vendor/../css/x.css`
    counted as owned there and as foreign in `_PageSheets`, and the two
    disagreeing lists produced a false clear (Codex P2, #2038).
    """
    resolved = _resolve(href)
    if resolved is None or resolved.parent != (WEBUI / "css"):
        return ""
    return resolved.read_text()


class _PageSheets:
    """Every stylesheet a page actually applies, classified and in load order.

    ONE builder, called by every test that needs it. Three copies of this loop
    existed and had already diverged: the guard-the-guard test counted only
    LINKS, so a page whose single Genesis sheet is an inline `<style>` reported
    `owned=0` and was named as unchecked by the very test that exists to prove
    pages are checked — while the per-page test was checking it perfectly well.
    A question answered in three places is answered differently in three places.

    Every refusal below is UNCONDITIONAL. An earlier version ran the
    external-link check only when the page had no readable owned/vendor pair,
    which meant adding a CDN layout sheet to any real page sidestepped it
    entirely — MEASURED: 46 tests green with an unread external stylesheet in the
    document. A refusal that fires only in the cases nobody reaches is not a
    refusal.
    """

    def __init__(self, page: str, html: str) -> None:
        self.page = page
        sheets, not_applied = _sheets(html)

        # 1. Sheets the browser does not unconditionally apply.
        assert not not_applied, (
            f"{page} carries a stylesheet that is not unconditionally applied: "
            f"{not_applied}. Its declarations are live under some condition and "
            "not others, so counting them as answers clears a leak that survives "
            "everywhere else, and ignoring them reports one that does not exist. "
            "Drop the condition, or state the exemption here."
        )

        hrefs = [v for kind, v in sheets if kind == "link"]

        # 2. Links to another origin. Checked BEFORE the lists are built, so it
        #    cannot be sidestepped by a page that still has a local pair.
        external = [h for h in hrefs if _is_external(h)]
        assert not external, (
            f"{page} links stylesheets on another origin ({external}). This guard "
            "cannot read them, so anything they declare is invisible and the page "
            "is UNCHECKED rather than clean. Vendor the sheet under webui/, or "
            "state the exemption here."
        )

        # 3. Links that should name a file we ship and do not.
        missing = _unresolvable(hrefs)
        assert not missing, (
            f"{page} links stylesheets that name no file under webui/: {missing}. "
            "Either the page is broken or this guard cannot read the sheet it is "
            "supposed to check, and it must not quietly stand down over either. A "
            "version of this test dropped unresolvable hrefs, found no owned "
            "sheet, and SKIPPED: one `?v=` cache-buster disarmed a whole page and "
            "the run reported `25 passed, 1 skipped`, exit 0."
        )

        self.order: dict[str, int] = {}
        self.css: dict[str, str] = {}
        self.owned: list[str] = []
        self.foreign: list[str] = []
        inline_seen = 0
        for i, (kind, value) in enumerate(sheets):
            if kind == "inline":
                inline_seen += 1
                name = f"{page} <style> #{inline_seen}"
                text = value
                owned = True
            else:
                name = value
                resolved = _resolve(value)
                if resolved is None:  # unreachable: step 3 refused these
                    continue
                text = resolved.read_text()
                # Ownership from the RESOLVED path, not from the href's prefix.
                # `/css/../index.css` is a valid link the browser loads as the
                # VENDOR sheet, and a prefix check called it ours.
                owned = resolved.parent == (WEBUI / "css")
            self.order[name], self.css[name] = i, text
            (self.owned if owned else self.foreign).append(name)

    @property
    def checkable(self) -> bool:
        return bool(self.owned and self.foreign)

    def declarations(self, name: str) -> dict:
        return _declarations(self.css[name])


def _pages_linking_stylesheets() -> list[tuple[str, str]]:
    """(name, html) for every servable page that links a stylesheet.

    Sourced from `dashboard_pages.stylesheet_pages`, NOT from a glob of the
    template directory. The login page is built as a Python string, so a glob
    omits it — and had done so twice before this file was written, then a third
    time in this file's first version.
    """
    return sorted(
        (name, html) for name, html in stylesheet_pages().items() if _linked_stylesheets(html)
    )


PAGES = _pages_linking_stylesheets()


def test_the_template_scan_found_the_pages():
    """Guard the guard: an empty or shrinking population makes everything pass."""
    assert len(PAGES) >= 6, (
        f"expected at least the five top-level pages and the login page, found "
        f"{len(PAGES)} — the link parse or the page enumeration is broken, not the CSS"
    )
    names = {name for name, _ in PAGES}
    assert "genesis_dashboard.html" in names and "genesis_voice.html" in names, (
        "the two pages that use `.panel` must both be in scope"
    )
    assert "auth.py::_LOGIN_HTML" in names, (
        "the login page links the same vendor and Genesis sheets and is NOT a "
        "template, so it is the page every glob-based check has missed — three "
        "times now. It is in scope through dashboard_pages.stylesheet_pages()"
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
        for h in _linked_stylesheets(dict(PAGES)["genesis_dashboard.html"])
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


@pytest.mark.parametrize("page,html", PAGES, ids=[n for n, _ in PAGES])
def test_no_vendor_layout_rule_is_left_unanswered(page: str, html: str):
    """Every vendor layout declaration on a shared selector has a later answer.

    Fails the way the `.panel` defect should have failed: naming the page, the
    selector and the property, before anyone has to look at the rendered page.
    """
    sheet = _PageSheets(page, html)
    if not sheet.checkable:
        pytest.skip(f"{page} carries no owned/vendor pair we ship")
    order, owned, foreign, css = sheet.order, sheet.owned, sheet.foreign, sheet.css

    owned_decls = {h: _declarations(css[h]) for h in owned}
    # Selectors any Genesis sheet on this page lays out — the scope for the
    # embedded-selector refusal below.
    genesis_layout_keys = {
        key
        for h in owned
        for key, props in _declarations(css[h]).items()
        if set(props) & LAYOUT_PROPS
    }
    for href in foreign:
        vendor = _declarations(css[href])
        assert vendor, (
            f"{href} parsed to zero rules. A stylesheet that cannot be parsed "
            "reads as a sheet with no leaks, which is the wrong direction — "
            "check for an unclosed brace before believing this page is clean."
        )
        # Only where the embedded text names a selector Genesis LAYS OUT. The
        # unscoped version fired on `.xterm .xterm-accessibility:not(.debug)` —
        # a vendor pseudo naming nothing this repository styles — which is how a
        # refusal becomes noise and then gets deleted.
        embedded = sorted(
            f"{sel} (embeds {hit})"
            for sel, props in vendor.items()
            for args in _embedded_selectors(sel)
            for hit in sorted(k for k in genesis_layout_keys if k and k in args)
            if any(
                (sel, prop, r["value"]) not in BENIGN
                for prop in set(props) & LAYOUT_PROPS
                for r in props[prop]
            )
        )
        assert not embedded, (
            f"{href} sets a layout property through a selector that embeds another "
            f"selector: {embedded}. Identity cannot relate `:is(.panel)` to a "
            "`.panel` answer, and teaching it to is the coverage model this file "
            "deleted after four rounds of it failing open. Flatten the vendor rule, "
            "or answer it on the identical selector."
        )
    problems: list[str] = []

    for f_href in foreign:
        for sel_key, props in _declarations(css[f_href]).items():
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
        f"{page}: a vendor stylesheet's layout rule wins on a selector "
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
    voice = _linked_stylesheets(dict(PAGES)["genesis_voice.html"])
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
    # Sourced the same way the per-page test sources its sheets — shared
    # stylesheets AND the inline blocks, which are Genesis layout claims too.
    # Reading only `css/*.css` here while the per-page test reads inline blocks
    # as well would make one test's "answered somewhere" disagree with the
    # other's "answered here", which is how an asymmetry check acquires one.
    answered_somewhere: set[tuple[str, str]] = set()
    for _name, page_html in PAGES:
        for kind, value in _sheets(page_html)[0]:
            text = value if kind == "inline" else _owned_sheet_text(value)
            for sel_key, props in _declarations(text).items() if text else ():
                for prop, rules in props.items():
                    if prop in LAYOUT_PROPS and any(r["top"] for r in rules):
                        answered_somewhere.add((sel_key, prop))

    gaps: list[str] = []
    for page, html in PAGES:
        sheet = _PageSheets(page, html)
        if not sheet.checkable:
            continue
        order, owned, foreign, css = sheet.order, sheet.owned, sheet.foreign, sheet.css
        owned_decls = {h: _declarations(css[h]) for h in owned}
        for f_href in foreign:
            for sel_key, props in _declarations(css[f_href]).items():
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
                        f"{page}: {f_href} sets `{prop}` on `{sel_key}`, which "
                        "Genesis answers on other pages but not on this one"
                    )

    assert not gaps, (
        "a vendor layout rule is neutralised on some pages and not others:\n  "
        + "\n  ".join(sorted(set(gaps)))
        + "\n\nMove the answer to a stylesheet every affected page loads "
        "(css/components.css) rather than answering it per page."
    )


def _leaks(css: str, prop: str) -> list[tuple[str, dict]]:
    """Every rule a constructed stylesheet declares for `prop`, with its key.

    The key is DERIVED rather than passed in. It used to be an argument because a
    key was a separate concept — an approximation of which elements a selector
    reached. Now the key IS the selector, so asking a caller for both is asking it
    to state the same thing twice and get it wrong once.

    A LIST rather than one rule, because `body, html { … }` is two group members
    and therefore two keys. An earlier version asserted a single rule and failed
    on exactly the selector this file was built for.
    """
    found = [(key, r) for key, props in _declarations(css).items() for r in props.get(prop, [])]
    assert found, f"the constructed sheet declares no `{prop}`"
    return found


def _answered(answer_css: str, vendor_css: str, prop: str) -> bool:
    """Is EVERY member of the vendor sheet's selector list answered?

    Every member, not any: a rule that answers one half of `body, html` leaves
    the other half taking the vendor declaration.
    """
    answer = _declarations(answer_css)
    return all(_answers(_rules(answer, key, prop), leak) for key, leak in _leaks(vendor_css, prop))


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
        "display",
    ), f"`{answer_selector}` was accepted as an answer to `.panel` — {why}"


def test_the_vendors_own_selector_does_answer():
    """The other direction, so the test above cannot pass by refusing everything.

    A guard that rejects every answer is as useless as one that accepts every
    answer, and it is the easier of the two to ship by accident.
    """
    assert _answered(".panel { display: block }", ".panel { display: flex }", "display")


def test_a_selector_GROUP_answers_through_the_member_that_matches():
    """`#app .panel, .panel` answers a vendor `.panel`, through its second member.

    Recorded per group MEMBER rather than per rule, so a group is neither judged
    on its strongest member (which would accept an answer that does not apply
    everywhere) nor on its weakest (which would reject this).
    """
    assert _answered(
        "#app .panel, .panel { display: block }",
        ".panel { display: flex }",
        "display",
    )
    # And the same group does NOT answer a vendor rule on `#app .panel` through
    # its plain `.panel` member being present — identity is per member.
    assert _answered(
        "#app .panel, .panel { display: block }",
        "#app .panel { display: flex }",
        "display",
    )
    assert not _answered(
        ".panel { display: block }",
        "#app .panel { display: flex }",
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
        "overflow",
    )


def test_an_answer_scoped_to_an_AT_RULE_does_not_answer_at_every_width():
    """A vendor rule applies at all widths; an answer under `@media` does not."""
    assert not _answered(
        "@media (min-width: 900px) { .panel { display: block } }",
        ".panel { display: flex }",
        "display",
    )


def test_an_important_vendor_declaration_needs_an_important_answer():
    """Loading later is enough at equal importance, and not enough below it."""
    vendor = ".panel { display: flex !important }"
    assert not _answered(".panel { display: block }", vendor, "display")
    assert _answered(".panel { display: block !important }", vendor, "display")
    # And an important ANSWER is never required when the vendor is not important.
    assert _answered(".panel { display: block }", ".panel { display: flex }", "display")


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
        name: _unresolvable(_linked_stylesheets(html))
        for name, html in PAGES
        if _unresolvable(_linked_stylesheets(html))
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
    assert _answered(answer, vendor, "display")


def test_class_and_id_names_are_NOT_folded():
    """The other half, which a blanket `.lower()` would have got wrong.

    Class and id names ARE case-sensitive, so `.Panel` and `.panel` are two
    different selectors and an answer on one does not answer the other.
    """
    assert not _answered(".panel { display: block }", ".Panel { display: flex }", "display")


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
    assert _answered(answer, vendor, "display")


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
    counts PAGES, which was still six.
    """
    unchecked = [
        f"{page} (owned={len(s.owned)}, vendor={len(s.foreign)})"
        for page, s in ((p, _PageSheets(p, h)) for p, h in PAGES)
        if not s.checkable
    ]
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


# ---------------------------------------------------------------------------
# Round six. The estimate that survived the identity redesign lived UPSTREAM of
# it, in the key space, and decided whether a rule was looked at. Every test
# below pins either that collapse or one of the refusals that replaced a
# silently-dropped shape.
# ---------------------------------------------------------------------------


def test_every_vendor_layout_declaration_derives_a_key():
    """The collapse, measured against the real vendor sheet.

    The key used to be an APPROXIMATION of which elements a selector reached —
    last compound, classes only, bare elements, parenthesised arguments stripped.
    MEASURED on the shipped `index.css`: 30 of its 175 layout declarations, 17%,
    derived no key at all and were invisible to every check in this file.
    `#right-panel { display: flex }` was one of them.

    The key is now the selector itself, so this is 0 by construction — and this
    test exists to say so out loud if anyone reintroduces a reduction.
    """
    vendor = (WEBUI / "index.css").read_text()
    invisible: list[str] = []
    total = 0

    def walk(nodes):
        nonlocal total
        for node in nodes:
            if node.type == "qualified-rule":
                decls = [
                    d
                    for d in tinycss2.parse_blocks_contents(node.content)
                    if d.type == "declaration" and d.lower_name in LAYOUT_PROPS
                ]
                if not decls:
                    continue
                total += len(decls)
                if not _selector_keys(node.prelude):
                    invisible.append(tinycss2.serialize(node.prelude).strip())
            elif (
                node.type == "at-rule"
                and node.content is not None
                and node.lower_at_keyword in {"media", "supports", "container"}
            ):
                walk(tinycss2.parse_rule_list(node.content))

    walk(tinycss2.parse_stylesheet(vendor, skip_whitespace=True, skip_comments=True))
    assert total > 100, f"only {total} vendor layout declarations — is the sheet still there?"
    assert not invisible, (
        f"{len(invisible)} of {total} vendor layout declarations derive no key and "
        f"are invisible to this guard: {invisible[:5]}"
    )


def test_an_attribute_value_is_not_rewritten_by_combinator_normalisation():
    """The fail-open the previous round's own fix introduced.

    Collapsing whitespace with a regex over the finished selector text reached
    inside quoted values, so `.panel[data-slot="a > b"]` and
    `.panel[data-slot="a>b"]` normalised to the same string and answered each
    other. Token types cannot be fooled by their own contents.
    """
    assert not _answered(
        '.panel[data-slot="a>b"] { display: block }',
        '.panel[data-slot="a > b"] { display: flex }',
        "display",
    )
    # And the collapse it was added for still happens.
    assert _answered(
        ".wrap > .panel { display: block }", ".wrap>.panel { display: flex }", "display"
    )


def test_a_nested_AT_rule_is_refused_like_a_nested_qualified_rule():
    """The refusal added last round had a hole the shape it guards walks through.

    It looked for a nested QUALIFIED rule only, so a conditional one step down
    slipped past: the outer rule has no direct declarations, so it was skipped
    for being empty while the browser applied the rule inside.
    """
    with pytest.raises(AssertionError, match="NESTED rule"):
        _declarations(".shell { @media (min-width: 1px) { .panel { display: flex } } }")


def test_layer_carrying_layout_is_refused_rather_than_flattened():
    """`@layer` is not a conditional and must not be treated as one.

    Layer precedence outranks load order and importance — an important
    declaration inside a layer beats an important unlayered answer however the
    sheets load — so flattening it would report a leak answered by a rule the
    browser discards.
    """
    with pytest.raises(AssertionError, match="@layer"):
        _declarations("@layer vendor { .panel { display: flex } }")
    # A layer with no layout declaration is not this file's business.
    _declarations("@layer vendor { .panel { color: red } }")


def test_a_local_import_is_refused_and_a_font_service_import_is_not():
    """`@import` is BLOCKLESS, so the at-rule refusal never saw it.

    A local import pulls in a stylesheet nobody here parses, so every rule in it
    reads as absent. The one in the shipped tree points at a font service and is
    the only shape allowed through — CodeRabbit's suggested blanket refusal would
    have failed `index.css` at line 1.
    """
    with pytest.raises(AssertionError, match="@import"):
        _declarations('@import url("more-vendor.css");')
    _declarations('@import url("https://fonts.googleapis.com/css2?family=Rubik");')
    assert _declarations((WEBUI / "index.css").read_text()), (
        "index.css itself must still parse — it opens with a font-service @import"
    )


def test_a_media_conditional_link_is_refused():
    """A sheet linked `media="print"` answers nothing at any other width."""
    sheets, conditional = _sheets(
        '<link rel="stylesheet" href="/css/a.css" media="print">'
        '<link rel="stylesheet" href="/css/b.css" media="screen">'
        '<link rel="stylesheet" href="/css/c.css">'
    )
    assert conditional == ["/css/a.css (media=print)"]
    assert [v for _k, v in sheets] == ["/css/b.css", "/css/c.css"]


def test_inline_style_blocks_are_read_as_owned_sheets():
    """They are Genesis layout claims and were previously invisible.

    MEASURED: four of the six pages declare layout properties inline — 47
    selectors' worth on one of them. A claim the guard cannot see makes the
    vendor declaration it answers read as unclaimed, so the selector is skipped
    rather than checked. Position comes from the document, not from an
    assumption: every inline block in this repository happens to sit after every
    link, and the guard reads the order rather than relying on that.
    """
    sheets, _ = _sheets(
        '<link rel="stylesheet" href="/index.css"><style>.panel { display: block }</style>'
    )
    assert [k for k, _v in sheets] == ["link", "inline"]
    assert _declarations(sheets[1][1])[".panel"]["display"][0]["value"] == "block"

    pages_with_inline = [
        name for name, html in PAGES if any(k == "inline" for k, _ in _sheets(html)[0])
    ]
    assert len(pages_with_inline) >= 4, (
        f"expected inline <style> on at least four pages, found {pages_with_inline} "
        "— if they have been moved to shared sheets, that is good news and this "
        "test should be re-measured rather than relaxed"
    )


def test_a_vendor_selector_that_embeds_another_is_refused():
    """`:is(.panel)` reaches `.panel` elements and identity cannot say so.

    MEASURED: appending `:is(.panel) { display: flex; height: 100% }` to the real
    vendor sheet shipped silently with 37 passed, `.panel` answered in
    components.css all the while. Relating the two means deciding which selectors
    reach which elements, which is the model this file deleted.
    """
    assert _embedded_selectors(":is(.panel)") == [".panel"]
    assert _embedded_selectors(".shell :where(.panel)") == [".panel"]
    assert _embedded_selectors(".collapse:not(.show)") == [".show"]
    # The four the six-name denylist missed, each MEASURED shipping a live leak.
    assert _embedded_selectors(":host(.panel)") == [".panel"]
    assert _embedded_selectors(":host-context(.panel)") == [".panel"]
    assert _embedded_selectors("::slotted(.panel)") == [".panel"]
    assert _embedded_selectors(".x:nth-child(1 of .panel)") == [".panel"]
    # A formula is not a selector, and a plain pseudo carries nothing.
    assert _embedded_selectors(".x:nth-child(2n+1)") == []
    assert _embedded_selectors(".panel:hover") == []
    assert _embedded_selectors(":lang(en)") == []


# ---------------------------------------------------------------------------
# Round seven. The class: the redesign deleted every estimate about the
# RELATIONSHIPS between inputs and kept every estimate about WHAT THE INPUTS
# ARE. Each test below pins one member, with the mutation that demonstrated it.
# ---------------------------------------------------------------------------


def test_the_page_population_is_DERIVED_and_refuses_an_unknown_emitter():
    """A hand-list has a glob's defect with more entries: it omits silently.

    MEASURED: this module's first version listed two page sources. A review
    found a third — `routes/terminal.py`, serving `/genesis/terminal` with a
    vendor stylesheet and an inline `<style>` declaring
    `html, body { … overflow: hidden }`, the incident-one shape — and a grep
    found a fourth emitter besides. Four defects have now shipped through a page
    no enumeration happened to name.
    """
    from tests.test_dashboard import dashboard_pages

    found = dashboard_pages._emitters()
    assert found, "the emitter discovery found nothing — the marker or roots are wrong"
    assert found == set(dashboard_pages._PAGE_SOURCES), (
        "every discovered emitter must be mapped or exempted, and every mapping "
        f"must still emit: discovered={sorted(found)} "
        f"mapped={sorted(dashboard_pages._PAGE_SOURCES)}"
    )
    names = {name for name, _ in PAGES}
    assert "terminal.py::_TERMINAL_PAGE_HTML" in names, (
        "the terminal page links a vendor stylesheet and declares layout inline; "
        "it is the page that proved a hand-list is not a chokepoint"
    )


@pytest.mark.parametrize(
    "attrs,rel,why",
    [
        ({"media": "print"}, ["stylesheet"], "media"),
        ({"media": "(min-width: 900px)"}, ["stylesheet"], "media"),
        ({"disabled": ""}, ["stylesheet"], "disabled"),
        ({}, ["alternate", "stylesheet"], "alternate"),
    ],
)
def test_a_sheet_the_browser_does_not_apply_is_refused(attrs, rel, why):
    """One question — is this sheet live? — asked in ONE place.

    It was answered in four places from four partial rules and not at all in
    three more. MEASURED, each a live fail-open on the real pages: marking the
    sheet that carries the `.panel` answer `rel="alternate stylesheet"` left 46
    tests green, and so did marking it `disabled`.
    """
    assert why in (_not_applied(attrs, rel) or "")


def test_the_same_question_is_asked_of_a_style_element():
    """`<style media="print">` was recorded as an unconditional answer."""
    sheets, refused = _sheets('<style media="print">.panel { display: block }</style>')
    assert sheets == []
    assert refused == ["<style> (media=print)"]
    # And an ordinary one still counts.
    sheets, refused = _sheets("<style>.panel { display: block }</style>")
    assert [k for k, _ in sheets] == ["inline"] and refused == []


def test_ownership_comes_from_the_RESOLVED_path():
    """`/css/../index.css` is the vendor sheet, and a prefix check called it ours."""
    assert _resolve("/css/../index.css") == _resolve("/index.css")
    sheet = _PageSheets(
        "probe",
        '<link rel="stylesheet" href="/css/../index.css">'
        '<link rel="stylesheet" href="/css/components.css">',
    )
    assert sheet.foreign == ["/css/../index.css"], (
        "a link that resolves to the vendor sheet must be classified as vendor "
        f"however it is spelled; got owned={sheet.owned} foreign={sheet.foreign}"
    )


def test_an_external_link_is_refused_even_when_a_local_pair_survives():
    """The refusal ran only when the page had no readable pair, so any real page
    sidestepped it. MEASURED: 46 tests green with an unread CDN layout sheet in
    the document."""
    with pytest.raises(AssertionError, match="another origin"):
        _PageSheets(
            "probe",
            '<link rel="stylesheet" href="/index.css">'
            '<link rel="stylesheet" href="/css/components.css">'
            '<link rel="stylesheet" href="https://cdn.example.com/layout.css">',
        )


def test_an_external_import_is_refused_unless_its_host_is_allowlisted():
    """The exemption was "any absolute URL", so a CDN import was dropped whole."""
    with pytest.raises(AssertionError, match="@import"):
        _declarations('@import url("//cdn.example/layout.css");')
    _declarations('@import url("https://fonts.googleapis.com/css2?family=Rubik");')


def test_a_namespace_mapping_is_refused():
    """Prefixes are per-sheet, so identical text can target different elements."""
    with pytest.raises(AssertionError, match="@namespace"):
        _declarations('@namespace x url("http://www.w3.org/2000/svg");')


def test_an_unparseable_prelude_is_refused():
    """A stray brace is NOT reported as an error by tinycss2 — it is folded into
    the next rule's prelude, so that rule keys under the wrong selector.

    MEASURED twice: once as the fail-open (a leak behind one stray brace shipped
    with every test green), and once as a check written for the wrong token —
    the brace arrives as an `error` token, not as a literal whose value is `}`,
    so the first fix read as clean.
    """
    with pytest.raises(AssertionError, match="could not be parsed as a selector"):
        _declarations("}\n.panel { max-height: 120px }")


@pytest.mark.parametrize(
    "prop", ["flex", "flex-flow", "inset", "grid-template", "aspect-ratio", "contain"]
)
def test_the_layout_set_covers_the_shorthands_of_what_it_already_watches(prop):
    """`flex-direction` was watched and `flex` was not; `position` and not `inset`.

    MEASURED: `flex: 0 0 120px; inset: 0; aspect-ratio: 1/4` on the vendor
    `.panel` rule — three layout-deciding declarations — left 46 tests green.

    The set is still a deliberate SAMPLE and not a closed set. 136 non-custom
    properties in the shipped sheets sit outside it, of which roughly 69
    plausibly affect layout — `margin`, `padding`, `top`, `gap`, `transform` and
    the rest. Watching those would flood the guard into uselessness, so the line
    is drawn at properties that decide where a box IS at the level the two
    incidents were about, plus the shorthands and aliases of those. That the line
    is a judgement rather than a boundary is said here rather than implied.
    """
    assert prop in LAYOUT_PROPS
