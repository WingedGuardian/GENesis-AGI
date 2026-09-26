- `memory_recall(include_deprecated=True)` now returns graph neighbours for the
  superseded memories it surfaces, instead of returning the memory with an empty
  `graph_neighbors`. The traversal re-applied the visibility filter the caller
  had explicitly opted out of, so an audit or history recall could not see how a
  superseded memory was connected. `memory_expand` gains the same
  `include_deprecated` parameter, which it previously had no way to express. A
  superseded neighbour is labelled `"hidden": true` so it cannot be mistaken for
  a live one. The parameter un-hides superseded memories ONLY — a bitemporally
  expired memory stays out of the traversal, matching the search beside it, which
  applies its expiry filter unconditionally. Ordinary recall is unchanged:
  hiding what recall itself hides remains the default on every backend.
