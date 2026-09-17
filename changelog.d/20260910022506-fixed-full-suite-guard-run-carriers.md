- **A test run fronted by a package-manager runner no longer slips past the
  full-suite guard.** The guard that stops a whole-suite pytest run on a shared
  box keyed on the command being `pytest` itself, so `uv run pytest tests/`,
  `uvx pytest tests/`, `poetry run pytest tests/` and their hatch/pdm/xvfb-run
  equivalents sailed through while the bare form was blocked. The shell parser
  now sees through these runners to the command they carry — but only through
  their `run` subcommand, deliberately: treating the whole tool as a
  pass-through would have made the parser skip past the first word of *every*
  subcommand, hiding commands that other guards catch today. Any other
  subcommand still resolves to the tool itself, so `uv pip install pytest` and
  `poetry add pytest` install the package rather than looking like a run, and a
  package name handed to a flag (`uv run --with pytest ruff check .`) is read as
  the dependency it is. Replayed against 37,568 real commands from one install's
  history: one resolution changed, and it was a `poetry run python` the parser
  had been reading as `poetry` — the fix behaving, not a casualty.
- **An unfamiliar option no longer lets a whole-suite run through that guard.**
  These runners accept options before the command they carry, and some of those
  options take a value of their own. The parser recognised a list of them, and
  where an option was missing from that list its value could be mistaken for the
  command being run — so the guard looked at the wrong word, concluded the run
  was something else, and allowed it. Keeping such a list complete means tracking
  another project's options as they change, which is not a thing this parser can
  promise. It now stops at the first option it does not recognise and reports
  what it is sure of — that the command is being carried by one of these runners —
  which is enough for the guard to find the run. Commands that are not test runs
  are unaffected, and a familiar option still resolves exactly as before.
