- **A memory containing a known alias could be stored twice.** `MemoryStore`
  expands surface-form aliases (`"CC"` to `"Claude Code"`) before writing, but
  it did that AFTER the duplicate check. The duplicate check matches the
  full-text index exactly, and what lands in that index is the expanded text --
  so a save of `"CC owns the gate"` persisted `"Claude Code owns the gate"`,
  and the next save of the same raw text searched for the raw form, missed the
  row it had just written, and stored a second copy with byte-identical
  content.

  Aliases are seeded by default, so this needed no configuration. Normalization
  now runs before the duplicate check, so a repeated save is handed the same
  text the store will persist. It stays best-effort: a normalization failure
  still lets the store proceed with the raw text rather than raising.
