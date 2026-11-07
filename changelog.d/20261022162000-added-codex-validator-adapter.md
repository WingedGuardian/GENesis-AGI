- Added an inactive Codex validator adapter that runs existing safety guards,
  converts approval requests to explicit denials, retains advisories and refuses
  observable guard failures. Shell calls require an absolute directory change
  with a failure exit and newline before the body, preserving foreground scope;
  patches are checked against their actual targets in validator scratch roots.
  Publishing-allowance qualification keeps six original tests direct and checks
  validator refusal separately with independent cache state and fault controls.
  Ordinary Codex configuration and MCP access are unchanged.
