- **The worktree-removal guard no longer refuses a command for *mentioning* a
  removal, and no longer misses one that is really happening.** It matched raw
  command text, so the phrase inside a quoted grep pattern, a heredoc body, a
  docstring or a commit message read as the operation itself. That is not only
  friction: a blocked command is discarded *whole*, so a false match on the last
  step silently threw away the file writes in the earlier ones while the error
  mentioned only the rule that fired. The check now keys on the parsed command —
  the executable, its subcommand and its real arguments. Replayed over 51,052
  unique (command, directory) pairs from one install's own session history, each
  from the directory it was typed in: 154 commands blocked before and 154 after,
  but it is a different set — 6 refusals of commands that merely *mention* a
  removal are released, and 6 real removals that were previously allowed are
  caught. The unchanged total is why the composition is quoted rather than the
  count. That corpus is built from real transcripts and cannot be published, so
  these totals are the scale at which the change was observed rather than a
  result anyone else can reproduce; the composition is the claim.
- **A worktree removal hidden behind a global git flag is now caught.** The
  check required `git` to be followed immediately by the subcommand, so
  `git -C <path> worktree remove <target>` slipped past it entirely. Two shapes
  did: a global flag before the subcommand, and — found by cross-model review —
  a global flag whose *operand* happened to equal the subcommand's own name,
  which made the operand extraction anchor one token early and skip the segment.
  The parser now reports the subcommand's index from the same scan that finds
  it, so no caller has to re-derive it.
- **The global Bash safety hook is now syntax-checked by the test suite.** It is
  loaded for every session on the machine, and a shell syntax error there exits 2
  — which Claude Code reads as "block" — so a typo would have refused every
  subsequent command, including the one needed to repair it. Nothing checked for
  that before.
- **An ordinary `pip install` run from inside a worktree is no longer blocked.**
  The editable-install check looked for `-e` as a plain substring, so any pip
  command carrying a long option whose name begins with `e` supplied one —
  `--exclude-files`, `--extra-index-url`, `--exists-action` — as did a package
  name with `-e` inside it, such as `pytest-env`. Since the same check also fires
  when the current directory is a worktree, that hard-blocked routine installs
  from every worktree, and a blocked command is discarded whole. It now asks
  whether a `-e` *option* is present rather than whether those two characters
  appear anywhere.
- **Editable installs that were slipping through are now caught.** `-qe`, `-ve`
  and `--editable=<path>` all reach pip's editable code path and none of them
  contains a literal `-e`, so the old substring never saw them. Abbreviated long
  flags are covered too — pip installs from `--ed` just as it does from
  `--editable` — which the stricter check would otherwise have started missing.
  The same gaps existed in the project-level copy of the check and are closed
  there too.
- **A forced worktree removal is now caught however the flag is spelled.** The
  check wanted `-f` followed by a literal space, so a tab before the operand or
  the flag placed last did not count; and it wanted the long form spelled out in
  full, while git removes just as happily on `--f`. Measured against every
  spelling git accepts, the old check missed 39% of them and the new one misses
  none.
- **The shell-side checks stay deliberately over-matching about WHERE the command
  starts.** They run in the global hook with no access to the command parser, so
  keying them on command position means modelling shell grammar with a regex — an
  open set. An attempt to do so fell open on a leading redirection and on the
  `command` / `env` wrappers, all of which the broader form catches. They keep
  refusing some commands that merely mention an operation; that friction is the
  deliberate price of not leaving a hole in a check that exists because of a real
  crash. Recognising a flag is a separate question that needs no grammar, which
  is why the two fixes above do not reopen that hole.
- **A removal launched through a shell option bundle, or through a tool named by
  its full path, is caught again.** `bash -ce '<removal>'` runs the removal, but
  the parser read the bundle's trailing letter as the script and never looked
  inside; and the check for tools that carry a command string required the tool's
  name to start a word, so `/usr/bin/find … -exec <removal>` matched nothing. It
  now asks the parser, which has already resolved the executable, instead of
  re-reading the text.
- **A removal inside a command the parser cannot read is refused again.** When
  tokenizing fails the guard falls back to a coarse text match, and that fallback
  still required `git` immediately before the subcommand — so a global flag in
  between let it through on exactly the input nothing else could inspect.
