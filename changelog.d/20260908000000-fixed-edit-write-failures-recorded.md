- **Genesis notices when its own file edits fail.** The sensor behind the
  tool-call calibration base rate had recorded zero failures in roughly 25,000
  rows over eight weeks — not because nothing failed, but because on current
  Claude Code a failed `Edit` or `Write` fires no hook at all, so the failure
  path was unreachable and the base rate read a constant "everything succeeds".
  Both outcomes were in the session transcript the whole time. Genesis now scans
  that transcript when a turn ends and records every edit outcome, success and
  failure together as one population, so the calibration lane finally measures
  something real: on this install's transcripts the rate comes out near 0.95
  rather than a flat 1.0. Existing rows are left alone and re-scans are
  idempotent, so nothing is double-counted. Each recorded outcome also names the
  session it actually happened in — it previously stored the wrong session id, or
  none, for most rows.
