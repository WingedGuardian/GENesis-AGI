- **A hook guard whose own code fails to load now refuses the command instead of
  disappearing.** Claude Code treats any exit code other than 2 as a non-blocking
  error, so a guard that raised while it was still importing did not degrade — it
  vanished, while the session went on believing it was protected. Measured across
  every guard that shares the command parser: all of them, with a healthy control
  still refusing. They now fall back to a deliberately crude read of the raw command
  text and refuse anything that names a gated operation, over-broad by design and
  loud about it, rather than standing silently aside. A payload that names no command
  at all is refused on the same reasoning. The usual cause is a partially updated
  hook tree, and the refusal now says so.
