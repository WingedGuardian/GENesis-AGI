- Automatic indexing pauses before queue consumption while the protocol upgrade
  is incomplete. Requests and explicit queue APIs remain available; readiness
  observation cannot enable indexing or change queued work. Manual refreshes
  with `scripts/lib/code_intel_index.sh` keep working, and the paused runner
  reports a normal tick rather than a failed unit.
