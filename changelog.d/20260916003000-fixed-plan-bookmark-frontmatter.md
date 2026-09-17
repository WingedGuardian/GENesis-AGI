- **A plan's bookmark keeps its name when the plan has frontmatter.**
  `plan_bookmark_hook` names a bookmark by the plan document's first markdown
  heading, and that name is what `/unshelve` searches by keyword. It scanned
  only the first ten lines of the file, so a plan opening with YAML
  frontmatter pushed its heading out of range, the title came back empty, and
  the bookmark became unfindable — silently, since nothing raised and nothing
  logged. Frontmatter is now skipped before the scan, with the ten-line window
  kept (a heading far down a plan document is a section, not its title) and a
  bounded search for the closing fence so a leading `---` thematic break is
  still read as one.
