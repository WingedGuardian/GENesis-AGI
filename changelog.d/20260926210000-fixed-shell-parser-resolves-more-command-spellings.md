- **The command guards now recognise several shell spellings they previously
  could not see.** The shared shell parser that every Bash guard uses to work out
  which command a segment runs now handles a `NAME+=value` or subscripted
  `name[i]=value` prefix assignment, `env` assignment words that are not plain
  identifiers (including after `env --`), assignments after the bash `time`
  keyword, a wrapper's operands after `--` (`timeout -- 5 cmd`), and a command
  that follows an earlier one inside a subshell. It also no longer treats a word
  containing `=` as an assignment after wrappers such as `nohup` or `timeout`,
  which run that word as the program, and follows `sudo`'s own rule: a path
  containing `=` (`/opt/k=v/tool`, `~/k=v/tool`) is the program it runs.
  Parentheses inside quotes, comments and `${...}` expansions are no longer
  counted as subshell boundaries.
