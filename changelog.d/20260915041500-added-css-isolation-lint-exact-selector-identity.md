- **A vendor stylesheet's layout rule with no Genesis answer now fails a test
  instead of shipping.** Every page loads an inherited sheet before any Genesis
  one, and two of its layout rules land on selectors Genesis also uses. Neither
  errors, neither logs, and a page that renders wrong renders wrong quietly: one
  of them made a page unscrollable and was fixed by copying three declarations
  into each page that hit it, so the page that never hit it never got a copy;
  the other laid every panel's header down the left side of its panel as a
  narrow vertical gutter and went unnoticed for months. Both were found by
  someone looking at a page, which is the one check with no harness.

  The guard asserts, per page, that any layout property an inherited sheet sets
  on a shared selector is answered by a Genesis rule on the same selector, at the
  top level, in a sheet that loads later — and, across pages, that an answer
  present on one page is present on all of them, which is what the second
  incident actually was. Polarity is allowlist: a denylist of known-bad selectors
  would have passed cleanly for the entire time the panel rule was broken,
  because nobody knew to add it.

- **It compares selectors for identity rather than modelling the cascade, and
  that is a retreat from two models that were each attacked successfully.**
  Matching a bare class against a descendant form needed a rule for which one
  wins, so specificity was compared; specificity answers who wins where both
  rules apply and says nothing about where, so a hover-scoped answer was accepted
  while every element nobody was pointing at still took the vendor declaration; a
  coverage test was added on top of it.

  Both were estimates. The specificity estimate over-counted in five
  separately-constructed selector shapes, two of which accepted a live leak end
  to end. The coverage test compared flat token sets, which cannot see position
  or multiplicity, so a descendant, a child, a sibling and an id-qualified form
  of the same class all read as covering it — substituting the first of those for
  the shipped answer reintroduced the original incident with 26 tests green.

  Four consecutive reviews each found the model fail-open in a new shape, and the
  shapes had nothing in common except that a person had to think of them. An
  answer must now be written on the vendor's own selector, normalised for
  whitespace and group order. Two rules with the same selector apply to the same
  elements at the same specificity, so neither question needs estimating, and
  what remains — which sheet loads later, which declaration is important — are
  facts rather than approximations.

  The cost is stated rather than discovered: a genuinely covering answer written
  any other way now reads as unanswered. That is a false alarm, and the remedy is
  a table row naming the reason. The failure it replaces was silence.

- **An unresolvable stylesheet link now fails the guard instead of disarming
  it.** A link whose href named no file under the web root was dropped; with none
  left the page had no owned sheet, and the test skipped. One cache-busting query
  string on one link therefore turned the whole check off for that page and the
  run reported a skip and exited zero. Query strings and fragments are stripped,
  so an ordinary cache-buster resolves; anything still unresolvable is reported.

- **A stylesheet this repository does not ship makes a page unreadable, and the
  guard now says so instead of standing down.** A link to another origin is
  rightly excused from the resolvability check — it is not ours to ship — and it
  then failed to resolve too, so it fell out of both the owned and the vendor
  list and the page skipped by a second route. Moving one page's links to a
  content network gave a skip and exit zero on a page whose rules had not
  changed. Not ours to READ is not the same as clean.

- **A clearance is for a declaration, never for a slot.** The table of vendor
  declarations that need no answer was keyed on selector and property, while the
  comment beside it said, correctly, that the same property at a different value
  would be a real leak and must still fail. Changing the vendor's `width: 100%`
  on the page root to `250px` was cleared by a row written for the other number,
  with every test green. Rows now carry the value they excuse.

- **Type selectors are matched case-insensitively, as HTML defines them.** Keys
  and comparisons were case-sensitive, so rewriting the vendor's own page-root
  rule in capitals left the whole viewport lock in place with the suite green —
  one of the two incidents this guard exists for, reproduced with the shift key.
  Class and id names stay case-sensitive, which is why the fold walks tokens
  rather than lowercasing the text. Whitespace around a combinator is now
  insignificant too, so a minified vendor rule and a hand-written answer compare
  equal.

- **Shapes the reader cannot see are refused rather than dropped.** A rule nested
  inside another, and any block at-rule not on the descend list, previously
  vanished with everything in them; both now raise and name the remedy. The
  property set gained the near neighbours it was missing — a clip set through
  `max-height` passed while `height` was watched — and a stylesheet that parses
  to zero rules is reported instead of reading as a sheet with no leaks.

- **The last approximation is gone, and it was never in the comparison.** Six
  rounds of review each found this guard failing open in a new shape, and the
  pattern only became legible when the findings were bucketed by pipeline stage:
  none of them landed on the selector comparison the previous round introduced.
  They landed one stage earlier, on the "key" — an estimate of which elements a
  selector could reach, derived by reducing each selector to its last compound,
  its classes, or a bare element name. That estimate ran BEFORE the comparison
  and decided whether a rule was looked at, so its errors were silent by
  construction. Measured against the shipped vendor sheet, thirty of its
  hundred and seventy-five layout declarations — seventeen per cent — derived no
  key at all and were invisible to every check here, an identifier-selected panel
  rule among them.

  Since the comparison already works on normalised selector text, the key can be
  that text. One stage instead of two, no approximation left anywhere, and the
  last-compound split, the class-name pattern, the bare-element rule and the
  parenthesis stripper are deleted rather than fixed. Measured after: zero
  invisible declarations, zero new findings on the real stylesheets, both
  original incidents still caught. The round count had been tracking remaining
  estimating stages, not remaining bugs.

- **What the guard cannot read is now refused rather than skipped.** A rule
  nested inside another was refused already; a conditional nested inside a rule
  was not, so it walked through the refusal added for exactly that shape. Joining
  it: a cascade layer carrying a layout declaration, whose precedence outranks
  everything compared here; a local import, which pulls in a stylesheet nobody
  parses, while the font-service import the vendor sheet opens with is allowed
  through by name; a stylesheet link under a media condition, whose declarations
  answer nothing outside it; and a vendor layout rule whose selector embeds
  another selector, where a reader would reasonably expect an answer on the
  embedded one to count and identity cannot say so. The single real instance of
  that last shape carries a row explaining why it is inert.

- **Inline style blocks are read, having been documented as invisible.** Four of
  the six pages declare layout properties in a `<style>` block — forty-seven
  selectors' worth on one of them — and a Genesis layout claim the guard cannot
  see makes the vendor declaration it answers read as unclaimed, so the selector
  was skipped rather than checked. Blocks are now collected alongside links, in
  document order, and their position is read rather than assumed.

- **The login page is in scope, at the third attempt.** It is built as a string in
  Python rather than as a template, so every check written against a glob of the
  template directory has omitted it: once when the viewport fix was copied per
  page and it never got a copy, once while that very omission was being fixed,
  and once more when this guard was first written. The enumeration now lives in
  one place that both test files import, with the reason attached — a rule every
  call site has to remember is a rule the next call site will not.

- **The redesign deleted every estimate about how the inputs RELATE and kept
  every estimate about what the inputs ARE.** That is the sentence the last six
  review rounds were circling. Specificity and coverage — estimates about whether
  two selectors reach the same elements — were deleted and replaced by identity.
  Four other estimates were left standing, all upstream of the comparison, all
  deciding whether a rule was ever looked at: which pages exist, which sheets the
  browser actually applies, which properties count as layout, and which selector
  shapes are unreadable. Nine measured members, four of them live fail-opens
  demonstrated against the shipped stylesheets.

  Each is now closed the way the at-rule walk already worked — an allowlist with
  a refusal on the tail, so a shape invented next year fails loudly instead of
  vanishing.

- **The page population is derived rather than listed.** The previous fix
  replaced a directory glob with an explicit list of two page sources, which has
  the glob's defect and more entries: it omits silently. A review then found a
  third page — a terminal view serving a vendor stylesheet and declaring
  `overflow: hidden` on the document root, the first incident's exact shape — and
  a search of the tree found a fourth emitter besides. Emitters are now
  discovered mechanically and each must be mapped to the page it serves or
  exempted with a stated reason; an unmapped one fails, and so does a mapping for
  a module that has stopped emitting.

- **One function now answers whether a stylesheet is applied at all.** It was
  answered in four places from four partial rules and not at all in three more.
  Marking the sheet that carries the panel answer as an alternate stylesheet left
  every test green; so did marking it disabled; so did putting a media condition
  on an inline block rather than on a link. The same function is asked of a
  `<style>` element and a `<link>`, and an unrecognised condition raises rather
  than being guessed at.

- **Refusals that fired only in the cases nobody reaches now fire
  unconditionally.** The external-stylesheet refusal ran only when a page had no
  readable local pair, so adding an unread content-network layout sheet to any
  real page sidestepped it entirely. The import exemption admitted any absolute
  URL rather than the font service it was written for, so a content-network
  import was dropped whole. Both measured green before, both refused now.

- **Three smaller members, each measured.** Ownership is classified from the
  resolved path, because a link spelled with a dot segment resolves to the vendor
  sheet while a prefix check called it ours. A stray brace is not reported as a
  parse error — the parser folds it into the next rule's selector, so that rule
  keys under the wrong name and its leaks read as absent; preludes that did not
  parse are refused, and the check reads the token type the parser actually
  produces rather than the one it seemed like it should. And `@namespace` is
  refused outright, since prefixes are per-sheet and identical selector text in
  two sheets can target different elements.

- **The layout-property set gained the shorthands of what it already watched,
  and an honest statement of what it is.** A flex shorthand bypassed the
  longhand; an inset shorthand bypassed position. Those are closed at no cost —
  zero new findings on the real stylesheets. The set is still a deliberate
  sample: 136 non-custom properties in the shipped sheets sit outside it, of
  which roughly 69 plausibly affect layout, and watching those would flood the
  guard into uselessness. That the line is a judgement rather than a boundary is
  now said rather than implied.

- **The refusal list in the module docstring was four claims larger than the
  code.** A reader trusts that list instead of re-checking, which is the same
  mechanism that let the second incident ship — a page that obviously had a copy.
  Every entry is now pinned by a test that constructs the shape it names.

- Measured both directions throughout: fifteen of fifteen, nine constructed leaks caught and six correct trees left alone, plus every replay from every previous round still firing. The collected-test floor is re-derived again.
