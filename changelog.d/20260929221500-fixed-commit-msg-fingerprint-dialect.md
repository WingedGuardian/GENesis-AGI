- **The commit-message hook reads the install's private-identifier fingerprints
  the way the pre-push review does.** Both read the same fingerprint file, but the
  hook used `grep -E` and the pre-push review uses Python's `re`, so they disagreed
  about the same pattern. A pattern using Python-only syntax (`\A`, `\d`, a
  lookbehind) matched nothing at commit time. A pattern `grep -E` rejected (an
  unbalanced parenthesis) made the whole file read as "no match", so one bad line
  turned off every fingerprint, and a file saved with CRLF line endings matched
  nothing. The hook now reads the file with the pre-push rules, in Python: each line
  trimmed, comments and blank lines skipped, and a pattern Python cannot compile
  matched as literal text, named in a warning by line number. It prints line numbers
  only, never a pattern (the pattern is the private value) or the file's path (which
  can name a user or a host). A line that is not UTF-8 is still checked, byte for
  byte, instead of stopping the whole check. Without a working `python3` it falls back
  to `grep -E`, one pattern at a time, says the check is degraded, and names the lines
  that use syntax grep reads differently from Python. An `errexit` or `pipefail`
  inherited through `SHELLOPTS` or `BASH_ENV` no longer turns a clean commit into a
  failed hook.
