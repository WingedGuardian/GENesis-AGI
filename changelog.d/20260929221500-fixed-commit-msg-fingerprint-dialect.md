- **The commit-message hook no longer goes blind on a fingerprint line it cannot
  read.** It checks the install's private-identifier fingerprints with `grep -E -f`.
  A pattern `grep -E` rejects outright (an unbalanced parenthesis) made that read
  exit 2, which the hook took as "no match", so one bad line turned off every
  fingerprint in the file. A pattern it only warns about (a PCRE lookbehind, which
  Python's `re` accepts and POSIX ERE does not) never matched anything, and a file
  saved with CRLF line endings matched nothing at all, silently. The hook now reads
  the file the way the pre-push review reads it: surrounding whitespace (the `\r`
  included) is trimmed, and a line `grep -E` cannot use is matched as literal text,
  as the pre-push review does for a line Python cannot compile, so the other lines
  keep working. The hook names such lines by line number with how to rewrite them,
  and never prints the pattern itself, since it is the private value. The whole
  file is checked in one `grep` first, so a clean file costs what it did before.
