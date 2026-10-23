# gh listing caps

- **A `gh` listing is ALREADY capped before you pass a flag, and the caps are not
  uniform.** MEASURED by reading `--help` on gh 2.98.0 (2026-08-20): `pr list` 30 ·
  `issue list` 30 · `run list` **20** · `workflow list` **50** · `gist list` **10** ·
  `release list` 30 · `repo list` 30 · `cache list` 30 · every `search` subcommand 30.
  So `gh pr list --limit 30` is behaviourally identical to passing nothing, and a
  session once reported its own cap back as the repo's open-PR count (said 30, the
  real number was 78). The unflagged form is the dangerous one precisely because
  nothing in the command hints a cap is in force. `scripts/hooks/capped_read_advisory.py`
  now says so pre-flight, and a drift test re-reads `--help` so this table fails loudly
  when gh moves a number rather than quietly naming a cap that no longer exists — treat
  the numbers above as the reading at that version, not as durable facts. Raising
  `--limit` gets you MORE ROWS, and `--paginate` is a `gh api` flag every one of these
  subcommands rejects (MEASURED on gh 2.98.0: `unknown flag: --paginate`).

  **A SHORT read is NOT proof of completeness, and an earlier version of this very
  bullet said it was.** "Re-read until the result comes back short of your limit" is
  the intuitive rule and it is false, because GitHub shortens a response on its own
  for reasons the command cannot see. MEASURED, three independent ways: a FILTERED
  `gh run list` (`--branch`/`--created`/`--event`/`--status`/`--user`) is served by the
  workflow-runs endpoint, which returns at most 1,000 results for such a search;
  `gh pr list --search` and `gh issue list --search` route through GitHub search and
  stop at 1,000 (`gh pr list --help` advertises `-S, --search`); and any `gh search`
  whose query TIMED OUT returns fewer rows than asked for with
  `incomplete_results: true`. Each is short, and none is complete. `gh search` also
  refuses a limit above 1,000 outright (`` `--limit` must be between 1 and 1000 ``),
  so you cannot widen past it at all.

  So: a SATURATED read supports "at least N" and never "N" — that much still holds.
  For an exact count, use a source that reports a TOTAL rather than the length of a
  list you asked for, and check that total is not itself partial: the Search API
  returns `total_count` alongside `incomplete_results`, and the count is exact only
  when that flag is `false`. Date-slicing a query is still the way to get under a
  ceiling; just do not treat a short slice as self-certifying.
