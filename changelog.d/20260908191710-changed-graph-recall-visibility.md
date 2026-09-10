- **Graph-enriched recall no longer surfaces memories that recall itself
  hides.** When a memory was returned, Genesis also showed its graph
  neighbours — but that traversal never applied the visibility filter the rest
  of recall uses, so consolidated-away and expired memories were presented as
  live context. They are now filtered out of the graph entirely. On this
  install that changed the neighbour list for roughly a quarter of enriched
  results, and 6.5% of them turned out to have had neighbours that were
  *entirely* hidden memories.
