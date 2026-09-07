- **An update that leaves part of the system broken now says so.** After every
  update Genesis checked which subsystems had come up — except it asked the health
  endpoint for a list that endpoint has never returned, so the answer was always
  "nothing wrong" and the check had never once reported anything on any install.
  It now reads the record the server writes when it finishes starting, and compares
  it against the same record from before the restart. The comparison is what makes
  it trustworthy: a subsystem's own status is ambiguous, because most of them catch
  their own startup errors and end up described the same way as one whose optional
  dependency is simply absent. Something that was working before the update and is
  not working after it is unambiguous. Anything that got worse is named in the
  update history and in the update's own output, while a component that was already
  dormant — no optional dependency installed, nothing to start — stays quiet
  instead of crying wolf on every update.

  Findings are recorded, not treated as failures: one optional subsystem not
  starting is not a reason to undo an otherwise good update. The subsystems the
  server genuinely cannot run without were already covered — without them it does
  not finish starting at all, and the update rolls itself back.

  The server now also records which process wrote that startup record, so the
  update can tell its own server's report from one written by another Genesis
  process on the same machine at the same moment. When it cannot tell, it says so
  rather than reporting all-clear — including on the first update after this
  change, which has no earlier record to compare against.
