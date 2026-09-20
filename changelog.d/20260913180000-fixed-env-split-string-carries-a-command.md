- **A command written inside `env -S` is no longer invisible to the safety
  guards.** One of the small helper programs the guards look through accepts a
  whole command line packed into a single quoted argument, and runs it. The
  parser had no idea: it read that entire line as the *name* of a program, so
  the guards were shown something matching nothing they check, and a command
  they would normally refuse went through. They now see the command it carries.
  Getting this right meant following the helper's own rules rather than
  guessing at them — it has its own way of writing a space inside a word, its
  own comment marker, a way to cut the line short, and it treats anything
  written after the quoted part as extra arguments. Where the helper itself
  would refuse the line, because of an unrecognised escape or an unclosed
  quote, the parser now reports nothing rather than inventing a command out of
  input the system rejects. Replayed against 68,703 real commands from one
  install's history: no command's reading changed except the ones this fixes.
