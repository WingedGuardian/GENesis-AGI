- **Every panel on the dashboard gets its header back, and the voice page can
  scroll.** Panel titles had been rendering as a narrow vertical strip down the
  left-hand side of their panel, with the title itself floating level with the
  middle of the body rather than above it, and the panel's content pushed into
  the remaining space. On a full-height panel that put the title hundreds of
  pixels below the top of the box it names, often past the bottom of the window
  — so panels looked untitled, with a wide empty gutter beside them.

  The cause was a layout rule in the inherited base stylesheet, written for a
  different application's side-by-side panes, landing on a selector this
  dashboard also uses. The dashboard's own rule for the same selector never said
  anything about layout, so there was nothing for it to override. Nothing errored
  and nothing logged; it simply looked wrong, on every tab, for months.

  Looking for other instances of the same leak found a third: the base stylesheet
  also pins the page root to a fixed, non-scrolling viewport. Four pages undo
  that, in four separate places — and the voice page does not, so it could not be
  scrolled at all below the first screenful. Both neutralisations now live on the
  stylesheet every page loads, rather than being copied per page, which is what
  let one page be forgotten.

  Both are paired with a check over the stylesheets a page actually links: a
  layout property the inherited sheet sets on a selector Genesis also lays out
  must be answered by a Genesis sheet loading after it, and an answer that exists
  on one page must exist on all of them. What that catches is stated with it —
  it reads which declarations exist and in what order the sheets load, not which
  one a browser would pick, and a leak onto a selector Genesis has never styled
  is outside it. It would not have found the voice page on its own; a person did.
