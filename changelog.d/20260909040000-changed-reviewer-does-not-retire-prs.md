### Changed

- A reviewer session now flags a premise-level defect with
  `needs-architecture-session` and leaves the PR open, rather than retiring it.
  Retiring a PR (`gh pr close`) belongs to the session that takes up its
  revival. An open PR carrying the evidence is a live handoff; a closed one is
  an archaeology task.
