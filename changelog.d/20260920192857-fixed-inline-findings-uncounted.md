- The merge report no longer says `inline-findings: ok` when review findings
  went unscored. Some findings never count toward the blocking score by design —
  a review bot the gate does not recognise, a comment below Major, an anchor
  outside the diff or on a documentation path — and the row said `ok` because the
  score was zero, which is true and is not what it was read to mean. It now
  carries how many were not scored, pointing at the detail already printed above
  it. Where a count cannot be trusted — some reviewer output cannot be parsed
  into findings, or a review channel under-reported — the number is shown as a
  floor rather than an exact total, and both causes are named when both apply,
  because only one of them is recoverable by re-running.
- A review is no longer silently ignored because the gate does not recognise who
  wrote it. A second bot, or a human collaborator, could leave a finding on the
  diff or in a review body and reach nothing the report mentioned, so the row
  still read `ok`. Those are now counted and named. They are never scored and
  never block — the gate's job here is to make sure the reviewer is *seen*, and
  judging what they said is the reader's. Recognising a format is still what
  earns a weight: an unrecognised comment may carry five findings or none, so
  wherever one contributes, the reported total is shown as a floor (`3+ items`)
  rather than an exact count of findings.
- Nothing about what blocks a merge has changed, and a stranger still cannot
  make it block: only a recognised reviewer's findings are read for severity.
