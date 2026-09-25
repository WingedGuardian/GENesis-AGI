- `memory_recall(include_deprecated=True)` now returns graph neighbours for the
  hidden memories it surfaces, instead of returning the memory with an empty
  `graph_neighbors`. The traversal re-applied the visibility filter the caller
  had explicitly opted out of, so an audit or history recall could not see how a
  superseded memory was connected. `memory_expand` gains the same
  `include_deprecated` parameter, which it previously had no way to express.
  Ordinary recall is unchanged — hiding neighbours that recall itself hides
  remains the default on every backend.
