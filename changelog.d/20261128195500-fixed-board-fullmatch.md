- **Board identity validation no longer accepts a trailing line feed.** The raw
  CRUD validators now require a full-string match for 32-hex ids, repositories and
  card targets, so a value one character longer than the canonical identity is
  refused instead of committed. Valid identities, case folding and MCP whitespace
  normalization are unchanged.
