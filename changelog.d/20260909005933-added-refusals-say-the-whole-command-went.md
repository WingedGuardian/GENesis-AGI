- **A refused command now says that the WHOLE command was discarded, not just the
  step that was refused.** A blocking hook cancels the entire Bash call, so
  `cat > config.py <<'EOF' … EOF && git commit`, refused for the commit, also loses
  the write — and the message named only the commit, which reads as "the commit
  didn't happen" rather than "and the edit you just made never happened". Every
  guard that can refuse a Bash command now adds a line saying so, and approval
  prompts carry the same warning in the dialog, since declining skips every other
  step too.

  The note deliberately names nothing — not the files, not the steps. Working out
  which files a command would have written means deciding what each tool's flags
  do, a set with no closed boundary that produced fourteen review findings.
  Listing the parsed steps instead looks safer and is not: the shared parser
  splits on `|` as well as `&&`, and drops a plain redirect target, so the case
  this exists for renders as `cat / PORT=8080 / EOF` — no filename, and heredoc
  body lines presented as commands. Stating the one fact true of every block
  needs no knowledge of any tool and cannot go stale.

  It appears on any command with more than one step, which is most of them, and
  is load-bearing on the subset that actually lost a write. Separating those
  needs the tool-semantics guesswork above, so the ubiquity is the price of the
  note being unable to mislead.
