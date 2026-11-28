- **The ambient snapshot helper no longer lets a config value become an ssh option.**
  A host user or address that starts with `-`, or contains whitespace or control
  characters, is refused before ssh runs, and `--` now marks the destination as an
  operand. The remote config is owner-controlled; this is hardening, not a reachable
  exploit.
