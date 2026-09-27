- **The decision registry rejects shapes it used to silently drop.**
  - Unknown top-level keys, unknown `fallback` keys and non-string decision or
    option names are now load errors. YAML reads a bare `1`, `yes` or `no` as
    a non-string, so these used to collide or be renamed.
  - The `decisions.local.yaml` overlay is found where every other config
    overlay is: `~/.genesis/config/` first, then beside the shipped file.
  - An overlay may only override shipped decisions, never add one.
  - A falsy non-mapping overlay is an error, not "no overrides".
- **Gate verdicts now name their fallback.** Legacy mode returns a new `legacy`
  verdict that routes to `fallback.legacy`, instead of an abstain that pointed
  at the typed fallback. A calibrated-mode score with no calibration version
  abstains, and `DecisionSpec.fallback_for()` resolves a verdict to its
  declared behaviour.
- **Three ego proposal decisions are registered:** reconcile, scope and
  realist. The registry header now says coverage is not yet complete.
