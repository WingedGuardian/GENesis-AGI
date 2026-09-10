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
