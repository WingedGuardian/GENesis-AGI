- The review-round doctrine now says one thing: **round 4 is terminal.** Past four
  reviewed heads the decision is to merge with the outstanding issues filed, or to
  send the change back for rework — a fifth round happens only where you
  explicitly authorise one, and that authorisation is asked again every round
  after. The retired `FINAL_ROUND_CAP = 7` is deleted rather than deprecated a
  second time: no gate consulted it, while its value had been chosen to avoid
  colliding with the four-head boundary, so a dead tier was still shaping the live
  one and reading as though a seventh round were real. The `final-round-accept`
  sigil is still recognised and still refused out loud, so an older checkout is
  told it is dead rather than silently ignored.
