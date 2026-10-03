- The `genesis-merge-gate` self-exclusion now filters the mirror check by name
  under ANY workflowName: an API-published verdict was observed attached to a
  check suite owned by a different workflow (GitHub assigns the suite, not the
  publisher), so the previous empty-workflowName requirement let the mirror
  double-count as `ci: red` on the local merge path.
