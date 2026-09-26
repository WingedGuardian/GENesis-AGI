- **A half-written retry profile no longer takes all routing dark.** A call site
  naming a retry profile whose definition was malformed — `retry:` with the name
  typed and no body yet, the shape an interrupted edit leaves — used to raise out
  of config parsing. `runtime/init/router.py` catches that, so the runtime came up
  with no router at all and every LLM call site dark, announced by a single log
  line while nothing in the config file looked wrong. Such a profile now falls
  back to `default` with a warning naming it, so one unfinished edit costs that
  profile's own retry policy instead of the whole system. A profile name that
  appears nowhere is still a hard error, because that is a typo rather than a
  half-edit, and this matches what a malformed *provider* already gets: skipped
  and substituted, never fatal. Applies to the shipped config and to a local
  overlay, including the case where the overlay mentions a profile the base config
  has never heard of — the name is captured before the overlay sanitizer removes
  it, since after that step a half-edit and a typo are indistinguishable.
