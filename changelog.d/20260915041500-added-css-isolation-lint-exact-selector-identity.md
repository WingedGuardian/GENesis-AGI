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

- Measured both directions on the real stylesheets: eleven constructed leaks all
  caught, including each of the four selector shapes the previous model accepted,
  both original incidents replayed by deleting their answers, the capitalised
  vendor rule, the value-substituted clearance, and the page moved to a content
  network; and six correct trees all left alone, including the answer reformatted
  to one line, its selector group reversed, an extra member added, and a
  cache-busting query string on a link.
