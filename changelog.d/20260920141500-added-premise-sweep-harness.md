### Added

- `scripts/premise_sweep.py` — a harness for measuring a design question before
  redesigning against it, encoding the "Measure, Do Not Choose" doctrine clause
  by clause so each one is mechanical rather than remembered.

  **Enumerate, don't pick.** The author supplies axes; the tool sweeps the full
  cross product. One axis is the CANDIDATE axis (the things being compared);
  the rest are environmental (the conditions each candidate must survive).

  **Pre-register.** The predicate, the decision rule, and what happens if
  nothing passes are all required fields. The rule is echoed and deliberately
  not graded — grading it in code would move the judgement into a config file
  and out of the reader's view.

  **Control the instrument.** Each control arm names a candidate value and is
  swept across every environmental cell using the same template as the run. The
  oracle must match the predicate everywhere; the no-op must not. Neither arm
  declares an expectation, so a no-op copied from the oracle cannot certify a
  harness that reproduces nothing. Where the no-op passes, the hazard does not
  exist in that cell: it is named INERT and excluded from the denominator
  rather than silently inflating every candidate's score. When the controls do
  not hold the tool prints **no results table at all** — a matrix from a broken
  instrument reads exactly like a real one, and a warning above it is what a
  reader skims past.

  **Execution status is not a classification.** What happened to the process
  (ran / errored / timed out) is tracked separately from what its output means,
  so a crashed or timed-out cell can never satisfy a predicate. Cells run in
  their own process group and a timeout kills the group, so a probe's
  descendants cannot outlive it and corrupt later cells. Nonzero exits count as
  data only when the spec declares them.

  **A remedy must be an axis value.** A spec may declare `proposed_remedy`; the
  harness reports whether that fix was actually swept and how its slice of the
  table did, naming any cell it failed in. An unswept remedy is labelled
  UNVERIFIED without voiding the run — the instrument is sound and the finding
  is real, only the fix is unmeasured. This closes an asymmetry in the method
  itself: a premise check measures its finding but asserts its remedy, so the
  fix rides on the finding's credibility without ever being graded.
