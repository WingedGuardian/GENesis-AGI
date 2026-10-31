- **The merge gate shows CodeRabbit's outside-diff findings again.** Since
  2026-09-14 CodeRabbit writes findings that fall outside a PR's diff as a bold
  `Outside diff range comments (N)` section with one collapsible entry per
  finding, and the gate's reader only knew the earlier layout: it read zero
  declared and zero found, so these advisory findings were never shown (32 across
  30 reviews in October, 18 of them Major). The gate now reads both layouts, and
  a section whose entries it cannot read still reports the shortfall.
