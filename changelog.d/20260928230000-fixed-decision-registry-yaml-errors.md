- **Every malformed registry file is now a `RegistryError`.** PyYAML's built-in
  constructors raise `ValueError`, `AttributeError` and `KeyError` on some bad
  input (an impossible date, `!!timestamp nope`, `!!bool abc`), and those
  escaped the registry's error contract. The loader now converts any failure
  at its YAML boundary.
- **The memory-volatility decision ranks instead of deciding.** Measured
  zero-shot against later evidence, it separates memories that went stale at
  AUC 0.60: useful for ordering which memories to re-verify first, not for
  acting on a single answer. Its spec now consumes `ordering`.
