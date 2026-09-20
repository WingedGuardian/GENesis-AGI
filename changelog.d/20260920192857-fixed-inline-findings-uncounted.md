- The merge report no longer says `inline-findings: ok` when review findings
  went unscored. Some findings never count toward the blocking score by design —
  a review bot the gate does not recognise, a comment below Major, an anchor
  outside the diff or on a documentation path — and the row said `ok` because the
  score was zero, which is true and is not what it was read to mean. It now
  carries how many findings were not scored, pointing at the detail already
  printed above it. When the scan exits before it can count anything the row says
  so, rather than reporting zero it never measured. Nothing about what blocks a
  merge has changed.
