### Added

- `scripts/premise_sweep.py` — a harness for measuring a design question before
  redesigning against it. Takes axes and sweeps the full cross product, so the
  cells are enumerated rather than hand-picked; records the predicate and the
  decision rule before any result exists; and requires two instrument controls.
  When those controls do not hold it prints **no results table at all**, because
  a matrix from a broken instrument reads exactly like a real one. Each control
  arm is specified as axis values and substituted into the same cell template as
  the sweep, so it cannot certify a code path the sweep never executes.
