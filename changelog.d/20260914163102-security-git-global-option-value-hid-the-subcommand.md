- **The guards no longer lose sight of a git command that begins with certain
  global options.** To work out what a git command does, the guards first walk
  past any global options in front of the subcommand. An option that takes a
  separate value has to be stepped over together with that value; an option the
  walk does not recognise is stepped over alone. Three options that really do
  take a value were missing from that list, so the value was taken for the
  subcommand and the real operation behind it was never seen. No unusual
  quoting was involved — an ordinary, fully written-out command was enough, and
  both a publish and a worktree removal could pass their guards this way.

  All three are now recognised, in the four places the list is kept. Anyone
  updating gets the fix by pulling; there is nothing to enable.

  The list is no longer maintained by hand. A new test asks the installed git
  itself which global options consume a value — sweeping the binary for
  candidates and probing each — and fails if any of them is one the walk would
  step over alone. It checks in that direction only, so a list carrying options
  your git does not have is still fine: git rejects those commands outright.
  A future git release that adds such an option now turns the build red instead
  of quietly reopening the gap.

  That test is also what found the third option. An earlier version of this fix
  covered two and asserted, in a code comment, that the full set could not be
  determined automatically. That was wrong, and it was the reason the third was
  missed — the claim is what justified stopping. Deriving the set takes about
  twenty seconds.
