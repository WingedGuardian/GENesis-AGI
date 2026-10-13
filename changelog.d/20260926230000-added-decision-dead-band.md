- **Threshold decisions abstain near their cut.** A decision site that acts on
  a probability threshold must now declare a `dead_band` around its cut and a
  `tie_rule` for scores landing exactly on a band edge. The new
  `DecisionSpec.gate()` returns act, decline or abstain, and abstains for any
  score inside the band and for every score outside calibrated mode. Near-cut
  decisions are not repeatable: re-sending the identical request to a pinned
  decision model can flip roughly one in five of them while the aggregate rate
  barely moves, so a site may not take a branch that a retry could reverse.
