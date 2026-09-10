- **A reply cannot forge the notes written beside it.** The reviewed text is
  wrapped in markers so a grader can tell the interaction apart from the
  system's own remarks about it — but the markers were a fixed pair, which a
  reply could simply write, ending the quoted region early and placing its own
  lines where the system's go. The markers are now derived per message and
  checked against it, so no message can contain the one that ends its own
  region. The user's message is wrapped the same way; it had not been wrapped at
  all, which is the same gap on the input an outside sender writes.
