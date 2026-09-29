- **Every malformed registry file is now a `RegistryError`.** PyYAML's built-in
  constructors raise `ValueError`, `AttributeError` and `KeyError` on some bad
  input (an impossible date, `!!timestamp nope`, `!!bool abc`), and those
  escaped the registry's error contract. The loader now converts any failure
  at its YAML boundary.
- **The memory-volatility decision is a ranked yes/no.** It now asks whether a
  memory is likely to stop being true within two months, and consumes
  `ordering`: its probability ranks memories for re-verification. Measured
  zero-shot against later evidence, this phrasing separates memories that went
  stale at AUC 0.66, better than the previous five-way choice (0.62 at best),
  and unlike a choice it needs no rule for turning an answer into a rank.
