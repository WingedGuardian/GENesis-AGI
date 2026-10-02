- **The commit-message hook reads the install's private-identifier fingerprints
  the way the pre-push review does.** Both read the same fingerprint file, but the
  hook used `grep -E` and the pre-push review uses Python's `re`, so they disagreed
  about the same pattern. A pattern using Python-only syntax (`\A`, `\d`, a
  lookbehind) matched nothing at commit time. A pattern `grep -E` rejected (an
  unbalanced parenthesis) made the whole file read as "no match", so one bad line
  turned off every fingerprint, and a file saved with CRLF line endings matched
  nothing. The hook now reads the file with the pre-push rules, in Python, and in the
  Genesis venv's interpreter, picked in the pre-push review's order (the main
  checkout's first, since a pattern can compile in one Python version and not another): each line
  trimmed, comments and blank lines skipped, and a pattern Python cannot compile
  matched as literal text, named in a warning by line number. It prints line numbers
  only, never a pattern (the pattern is the private value) or the file's path (which
  can name a user or a host). A line that is not UTF-8 is still checked, byte for
  byte, instead of stopping the whole check. With no usable Python (a broken install,
  since bootstrap builds the venv before it installs the hooks) the hook says loudly
  that the message was not checked, and does not block. An `errexit` or `pipefail`
  inherited through `SHELLOPTS` or `BASH_ENV` no longer turns a clean commit into a
  failed hook. The pre-push review's reader is hardened the same way: a pattern that
  breaks the compiler in any way (a huge repeat count, deep nesting) is matched as
  literal text, and a byte that is not UTF-8 no longer stops its scan, so one bad
  line cannot turn off every other fingerprint there either.
