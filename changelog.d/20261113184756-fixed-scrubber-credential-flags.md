- **Captured command lines redact credential flag values in more forms.** After
  a flag named for a credential (`--password`, `--passwd`, `--token`,
  `--secret`, `--api-key`, `--http-password` and similar) followed by a space,
  `=`, `:` or `is`, the telemetry scrubber now withholds the whole value:
  unquoted, quoted with spaces, or JSON-encoded at any depth. An unclosed quote
  withholds to the end of the captured text. After `-u`, `--user` or
  `--proxy-user` it withholds the password in a `user:password` pair and keeps
  the user name. Left as they are: `--password-file` and similar names, a `-p`
  port, a `-u` with no colon, a `date -u +%H:%M` format and an `rsync` or `scp`
  remote path. Text that names a credential flag, such as a commit message
  mentioning `--password`, loses the one word after it, and a colon-bearing
  `-u` value that is not a password (`docker run -u 1000:1000`) loses its
  second half.
  Still not redacted: short password flags (`mysql -p…`, `sshpass -p`), argv
  lists, grouped short flags such as `-sSu`, and a few quoting shapes (a value
  joined past a closing quote, a quoted value after an apostrophe earlier on
  the line, a quoted value spanning lines inside another quote).
- **Transcript analytics keep every redaction the base scrubber makes.** The
  stored-transcript adapter runs its whole-text pipeline on the base arms only
  (new `scrub_base` in the shared scrubber): which JSON spans it sets aside, the
  whole-text pass and the span-edge probe. The credential-flag arms then run over
  that output, so they only add redaction. The stamped adapter version moves to
  `scoped-json-6`, so stored text is re-scrubbed at the next ingest. Not yet
  redacted there: a credential flag inside a JSON string value written with
  escapes the whole-text pass does not read, such as a `\n`-joined command.
