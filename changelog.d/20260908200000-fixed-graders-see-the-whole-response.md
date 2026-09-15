- **Genesis's self-review now reads your whole reply before judging it.** After
  each interaction a background pipeline grades what happened — whether the
  request was met, what to learn from it — and writes the verdict to long-term
  memory. It was shown only the first 1,000 characters of a response, with no
  indication that anything had been left out — so an ordinary answer could arrive
  looking like one that stopped mid-sentence. Measured over a month of production
  traffic on this install, about two in five graded responses were being shortened
  that way. The limit is now a safety valve set above real traffic; if a very long
  response does have to be shortened, the middle is removed rather than the end and
  the gap is labelled. The graders are also given the response inside explicit
  markers, and told about a shortened or interrupted run only when there is an
  actual signal to report — and the "shortened" signal now comes from the
  pipeline's own record of what it did, rather than from searching the reply for
  a phrase. A reply that
  happens to discuss this mechanism (or an incoming message that quotes it back)
  can no longer make the grader believe the reply was shortened when it was not,
  and the label on a genuinely shortened reply no longer claims anything about
  whether the model stopped early.
