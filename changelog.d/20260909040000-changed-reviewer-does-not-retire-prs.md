- **A review session no longer closes a pull request it is not reviving.** When
  a PR turns out to be wrong at the premise rather than in its details, it now
  gets a `needs-architecture-session` comment carrying the evidence and stays
  open; closing it belongs to whoever picks up the redesign, at the moment they
  pick it up. An open PR with the reasoning attached is found by listing the
  queue, and its review threads stay where the conversation is happening — a
  closed one is found only if somebody remembers it existed. If a PR should be
  closed and nobody is picking it up, that is a question for the maintainer
  rather than a call the review station makes on its own.
