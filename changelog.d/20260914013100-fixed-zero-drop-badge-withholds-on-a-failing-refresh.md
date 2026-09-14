- **The Zero-Drop tab stops showing a confident count while its own refresh is
  failing.** The board already refused to print a number when the detector
  behind it was stale or blind. It did not apply the same rule to itself: once
  a board had loaded, a refresh that started failing left the body carrying a
  loud "Refresh is FAILING" banner while the badge above it still showed a
  figure, for a board that could by then be hours old. The badge now withholds
  the number and says which fault is responsible.

  The fix runs a little deeper than that panel. Every dashboard panel tracked a
  single value that meant both "what is my transport doing right now" and "did
  the last attempt fail", and starting a retry reset it — so any panel asking
  whether its data was trustworthy got "yes" for the whole duration of each
  retry. Worse, when a server returns errors for a sustained period the client's
  backoff grows until it outlives the poll interval, at which point every
  request is overtaken by the next one before it finishes and the failure is
  never recorded at all. A panel could then show a confident, hours-old number
  with no warning of any kind, indefinitely. A failure is now remembered until a
  request actually succeeds.

  It also names every fault that holds at once — "detector blind + refresh
  failing" — rather than showing only the most severe. The previous wording
  picked one, so anyone who fixed the fault they were shown would have seen the
  number return with a second fault still true.

  Separately, when the open-pull-request cache cannot be read at all, that part
  now says "Unavailable — … (not a zero)" in the same words as every other part
  of the board. It had been printing the bare reason, which reads like a
  statement of fact rather than a part that could not be read; the distinction
  was carried only by the text colour.
