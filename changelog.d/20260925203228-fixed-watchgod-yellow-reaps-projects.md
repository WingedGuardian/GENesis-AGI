- The cc-tmp watchdog no longer deletes an entire project directory because
  that directory's own timestamp looks old. A project directory's timestamp
  only moves when a session is created or removed inside it, so working in one
  project for a week without starting a new session there made it look
  abandoned, and the cleanup took every session it held. Staleness is now
  judged per session from the files inside it, and a session whose contents
  cannot be read is kept rather than deleted.
  The patterns this sweep matches with are now built safely. They are
  assembled from the temp directory's own path, which is configurable, and
  the search tool treats them as *patterns* rather than literal paths — so a
  bracket in a home directory name, or a stray trailing slash, silently left
  every pattern matching nothing at all, with no error and no warning, while
  stale sessions accumulated. The rule identifying a real session container
  also could not be expressed as a pattern in the first place: what looked
  like "digits only" actually meant "one digit followed by anything", so a
  cache directory whose name merely began with a digit was treated as a
  container of session directories. That rule is now checked per candidate,
  where it can be stated exactly. And the sweep now reports a failed
  enumeration from the enumeration that actually ran, rather than inferring
  it from a separate preliminary walk that could succeed while the real one
  failed — in both of its passes, so an unreadable directory can no longer
  produce an entirely empty log that reads as "nothing to reclaim".
