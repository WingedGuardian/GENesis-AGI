- **Edits to the files that steer a CI check, an approval gate or an outbound
  action get the strictest review bar.** A pull request touching one of these
  now classifies in the `critical` lane, the same as the code that reads it,
  instead of as ordinary config:
  - the leak-scan rules and history allowlist, the atomic-write baseline, the
    shipped-hook version ledger, and the mutation-sweep manifests (not on main
    yet: the sweep's own pull request adds the first, the second is planned);
  - the autonomy rules and approval policy, the autonomous-CLI policy and the
    payments config;
  - the board, contributor-worklog, marketing and career outreach, desktop
    takeover, entity adjudication and update configs, each of which can switch
    an outbound or irreversible action on.

  A test re-derives which files CI checks read, so a check that starts reading a
  new one, in a recognised spelling, fails until it is listed. `pyproject.toml`
  and other config files are unchanged.
