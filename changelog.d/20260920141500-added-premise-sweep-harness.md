### Added

- `scripts/premise_sweep.py` — a harness for measuring a design question before
  redesigning against it. Takes axes and sweeps the full cross product, so the
  cells are enumerated rather than hand-picked; records the predicate and the
  decision rule before any result exists; and requires two instrument controls.
  When those controls do not hold it prints **no results table at all**, because
  a matrix from a broken instrument reads exactly like a real one. Each control
  arm is specified as axis values and substituted into the same cell template as
  the sweep, so it cannot certify a code path the sweep never executes.

  A spec may also declare `proposed_remedy` — the fix it intends to recommend,
  written as a partial axis assignment. The harness then reports whether that
  fix was actually swept and how its slice of the table did, naming any cell it
  failed in. A remedy whose values were never swept is labelled `UNVERIFIED`
  rather than voiding the run: the instrument is sound and the finding is real,
  only the fix is unmeasured. This closes an asymmetry in the method itself — a
  premise check measures its finding but asserts its remedy, so the fix rides on
  the finding's credibility without ever being graded.
