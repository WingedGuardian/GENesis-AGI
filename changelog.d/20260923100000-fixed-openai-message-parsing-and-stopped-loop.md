- **The OpenClaw chat-completions endpoint no longer hangs for five minutes, or
  answers with a success status it does not mean, when Genesis is shutting
  down.** If the conversation loop had stopped but was still configured, a
  request would sit waiting for the full five-minute timeout while occupying
  one of only three concurrent slots, so a few such requests made the endpoint
  unresponsive for everyone. If the loop had closed outright, the failure
  arrived after the response had already begun, so the caller received HTTP 200
  with a generic "I encountered an error" body and nothing indicating the
  request had not been served.

  Both states are now refused immediately with 503, and the two possible
  refusals say which one happened rather than sharing a message.

- **A message split across several text blocks is no longer truncated to the
  first one.** Clients that send content as a list of blocks — the usual shape
  when a message accompanies an image or other attachment — had everything
  after the first text block silently dropped, so Genesis answered a shortened
  version of the question with no sign anything was missing. In the common
  case the discarded part is the instruction that follows the attachment. All
  text blocks are now used, separated by a blank line.

  Note the voice endpoints still carry their own copies of this parsing and are
  not covered by this change; see issue #2272.
