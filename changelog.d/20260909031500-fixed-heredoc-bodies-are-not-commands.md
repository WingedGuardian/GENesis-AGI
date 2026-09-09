- **The command guards no longer read here-document text as commands.** When a
  shell command feeds a block of text to another program (the `<<` form), that
  text is data, not something the shell runs — but the parser behind Genesis's
  safety guards was treating each line of it as a command in its own right. Two
  things went wrong as a result: a guard could refuse a perfectly ordinary
  command because of words that appeared in the text block, and, where the text
  contained a quote character, the parser could run the block together with what
  followed it, so a real command after the block was not examined separately.
  Measured against 45,763 real commands from this install's history: 6,690 use
  the `<<` form, and the great majority of those were parsed this way. Commands
  that do not use it are unaffected — all 39,073 of them parse identically.
  A block with no closing marker is deliberately left alone rather than assumed
  to run to the end of the input, so nothing can be hidden from the guards by
  omitting it.
