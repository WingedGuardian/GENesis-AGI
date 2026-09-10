- **One malformed character no longer stops Genesis reviewing an interaction at
  all.** Some text encodings allow a stray half-character that is technically
  invalid on its own. JSON accepts it, so it can arrive in an incoming message or
  in a reply, and the self-review pipeline measures text in bytes — so it stopped
  with an error before grading began, and every lesson from that interaction was
  lost. Text is now checked as it enters review, and anything unencodable is
  shown in a visible escaped form rather than crashing the pass or being silently
  dropped.
