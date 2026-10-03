- **The test suite no longer fills a RAM-backed temp filesystem.** pytest's own
  temporary tree is now placed under `~/tmp` (on disk) for every local run, not
  just runs launched from a session whose `TMPDIR` already pointed at the
  budget-policed working temp. Runs started from a systemd unit, a detached
  shell, or any context with `TMPDIR` unset previously fell through to the
  default `/tmp`, which on a standard install is a small tmpfs — one suite could
  put hundreds of megabytes into memory and trip the temp watchdog. CI runs and
  any run passing `--basetemp` are unaffected; set `GENESIS_BIG_TMP` to place the
  tree somewhere other than `~/tmp`.
