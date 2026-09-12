- **The rm-guard now reads a multi-line command the way the shell reads it,
  comments included.** It already joined a line continuation before checking a
  delete's flags and target, which is what the shell does — but it did so with a
  pattern that had no notion of a comment, and a comment ends at the newline rather
  than continuing past it. A trailing comment on a multi-line command could
  therefore make the guard read the two lines as one, and stop recognising what the
  second line was. It now tracks quotes and comments while joining, and where a
  comment begins is checked against the shell itself rather than inferred — both
  ways round, since a `#` that only looks like a comment start must not stop the
  join either. Which closing parentheses end a word is part of that question, and
  the shell's answer is not uniform: a `)` closing a process substitution, an
  extglob pattern or an array assignment leaves the word open, while one closing a
  subshell or an arithmetic command ends it, and one form is decided by a shell
  option rather than by syntax, so the setting this hook actually runs under is
  what settles it. Each form was checked against the shell rather than reasoned
  about, because an error in either direction lets a payload through — and the
  checking is what corrected the first two attempts, both of which had a member
  wrong. Every parenthesis is now classified from its own opener rather than
  inheriting from an enclosing one; a single depth count could express neither
  that nor the case where a comment swallows a bracket.

  Nothing else about the guard changes, and the measurement says so with its
  denominators. Structurally: a command with no line-joining backslash is
  returned byte-identical — verified rather than assumed, on all 106,415 distinct
  commands in this install's history at the time of the run, and independently
  fuzzed over 198,927 generated strings. Only the 1,838 (1.7%) that do contain one
  can be affected at all, and across those no verdict moves in either direction.
  The absolute counts drift as history accumulates; the 1.7% reachable fraction
  held across runs.

  That last figure is a realism check and not a coverage claim, and the
  difference matters: the shapes this fix deliberately does change are ones real
  history happens not to contain, so a corpus finding no movement is the expected
  result rather than evidence of no effect. What covers those is the enumeration
  above, each form checked against the shell one at a time.

  Quoting is tracked per nested command, not as one flag for the whole string.
  A command substitution runs a fresh command with its own quoting, so a quote
  belonging to that nested command does not close the word around it — and a
  single quote-state slot read the nested opener as the outer closer, left
  quote mode early, and then mistook ordinary quoted data for the start of a
  comment. The join was then skipped where the shell performs one, failing in
  the same direction as above: the flags split across the newline and the
  delete stopped being recognised. Both spellings of the construct are handled, along
  with parameter expansion, where the shell never starts a comment at all.
  Each was measured against the shell one at a time rather than generalised
  from the first, and the state each opens is asserted to close again — a
  context that never resets would disable comment recognition for the rest of
  the command, which fails in the other direction.
