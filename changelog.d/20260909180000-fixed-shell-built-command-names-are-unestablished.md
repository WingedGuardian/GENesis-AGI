- **A command whose operation the shell assembles is now treated as unknown
  rather than as harmless.** The shared parser behind the approval guards uses
  Python's shell tokenizer, which reproduces quoting faithfully but performs no
  expansion at all. A word built by the shell therefore parses cleanly into
  something other than what actually runs, and when that word is the one
  choosing the operation, the guards saw a tidy parse naming nothing they gate —
  the same answer they give for a command that gates nothing at all. The parser
  now marks such a segment and reports it, so an operation it cannot establish
  routes to the existing "ask a human" path instead of a confident all-clear.
  Two word-generating constructs are covered — substitution and brace expansion
  — and the rule for each is stated as what it accepts rather than as a list of
  the spellings that fool it, so spellings nobody has thought of are covered
  too. Two further constructs are named in the code as deliberately uncovered,
  with the reason for each, because the recurring mistake on this file is
  claiming a set is complete. Measured against 129,179 real commands from this
  install's history, 14 change how they are read; ordinary work that
  interpolates a variable into an argument, a path, or a `gh api` endpoint is
  untouched.
