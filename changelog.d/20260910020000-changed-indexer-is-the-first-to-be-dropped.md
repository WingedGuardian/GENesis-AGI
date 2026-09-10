- **If the machine runs out of memory, the code indexer is now the first thing
  dropped.** It is background work that can simply run again, unlike a session
  holding whatever you were in the middle of. Previously nothing expressed that
  preference, so the choice came down to whichever process happened to be using
  the most memory — which could well be your session. There is also a setting for
  anyone who wants to choose the priority themselves, and it now validates what it
  is given: a value written with a leading zero used to be read as a different
  number entirely, and an absurdly large one wrapped around and quietly became the
  *least* protective setting possible.
