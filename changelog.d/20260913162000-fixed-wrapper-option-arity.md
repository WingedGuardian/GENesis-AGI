- **Two ways of writing a command that the safety guards could not see.** The
  guards work out what a command actually runs by looking through the small
  helper programs people put in front of it — the ones that set an environment
  variable, apply a timeout, or feed arguments in from a list. To do that the
  parser has to know which of a helper's own options are followed by a value, so
  it can step over them and reach the real command. Two entries in that table
  were wrong in the direction that matters: they claimed an option takes a value
  when it only takes one optionally, so the parser stepped over the command
  itself and the guards examined its first argument instead. Both
  forms run, and both reached a guard that would otherwise have refused them.
  The two wrong entries are gone, and a test re-derives the table from each tool's
  own option parser on the machine it runs on — falling back to its documentation
  where the parser cannot be read — so an entry that stops matching reality fails
  the build rather than going quiet. Reading the documentation alone was not
  enough: one helper accepts an option that its own help text never mentions, and
  a check that trusted the help would have condemned a correct entry and invited
  a repair that reopened the hole. Replayed against 68,308 real
  commands from one install's history, 1,744 of which use these helpers: no
  command's reading changed except the ones this fixes.
