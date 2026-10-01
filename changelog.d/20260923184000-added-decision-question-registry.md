- **Decision question registry.** `config/decisions.yaml` is the single
  enumerable home for every decision Genesis makes between bounded choices —
  including ones currently made by a hardcoded constant standing in for a
  judgment. Each entry declares its primitive (`choice` / `score` / `noul`),
  the question wording, what the caller does with the answer (`consumes`), and
  what it falls back to when no decision backend is available. A
  gitignored `decisions.local.yaml` overlay is deep-merged on top, following
  the `model_routing.yaml` pattern.

  `consumes` is the load-bearing field: it turns "may this site branch on a
  probability?" from something a reviewer has to notice into a property the
  loader enforces. `consumes: threshold` requires an explicit threshold inside
  `(0, 1)`, a stray threshold on a non-thresholding consumer is rejected, and
  only thresholding sites need calibration at all — `argmax` and `ordering`
  consumers stay fully functional without it. High-cardinality choices must
  declare a `cardinality_strategy`, because options share a fixed token budget
  in encoder-based decision models and labels stop being distinguishable as
  the set grows.

  Registering a decision does not wire it. Every entry starts inert and is
  promoted individually on measured evidence that it beats its incumbent.
