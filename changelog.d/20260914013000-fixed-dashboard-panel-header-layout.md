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

  Looking for other instances of the same leak found a third, and then a fourth.
  The base stylesheet also pins the page root to a fixed, non-scrolling viewport.
  Four pages undo that, in four separate places — the voice page did not, so it
  could not be scrolled at all below the first screenful, and neither did the
  login page, which is built as a string in Python rather than as a template and
  had been overlooked twice for that reason. Both neutralisations now live on the
  stylesheet every page loads, rather than being copied per page, which is what
  let two pages be forgotten.

  Nothing automatic guards this class yet. A check that reads the stylesheets a
  page links was built alongside this fix and has been separated onto its own
  branch: it found the original defect from the text alone, and it was also
  found fail-open in four consecutive reviews, so it is not something to hold a
  verified one-declaration fix behind. Worth saying plainly because the absence
  is the status quo rather than a regression — the three instances above were
  each found by a person looking at a page.
