- **The graph projector no longer publishes a snapshot the database was never
  in, and no longer overwrites a newer one with an older one.** Building a
  projection reads the links and the visibility metadata separately, and each
  read was taking its own view of the database — so a change that touched both
  in one transaction could be half-seen, publishing an edge whose endpoint
  should have been hidden. Both reads now share one view.

  The claim on the right to publish had a matching gap. A projector takes a
  lease before it builds, and the lease expires on a timer so a crashed run
  cannot hold it forever — but nothing checked the lease was still held at the
  moment of publication. A build slower than its lease could therefore hand the
  claim to a second run and then overwrite that run's newer result, walking the
  graph backwards with nothing to notice. Publication now happens only while the
  lease is still held, and a build that lost it is discarded rather than
  published: whoever holds the lease has data at least as fresh, so refusing is
  recoverable where reversing is not.

  Two smaller corrections. A memory whose expiry timestamp is present but empty
  was hidden by the incumbent path and shown by the new one — the two backends
  disagreeing about the same row, which is the one thing this work exists to
  prevent. The same was true of a bare zero. Both are fixed, and the parity is
  now settled by running the incumbent query itself over the whole range of
  values that column can hold, rather than against a list of cases someone
  thought of. And the projection command answers an unreadable or missing
  database with one line and an exit code, the way it already answered an
  unreachable engine, instead of a stack trace.
