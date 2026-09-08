- **A push that would publish one of your install's own private values is now
  refused, not just flagged.** The pre-push privacy check already spotted these
  and printed a warning — but the warning arrived alongside the push it was
  describing, and a push cannot be taken back. CI scans every commit a branch
  adds rather than its net result, so a later commit that removes the value does
  not clear it; the only remedy left is abandoning the branch and opening a
  replacement pull request. The check now refuses that push up front, naming the
  file and line and never repeating the value itself.

  Only a match against your own `release-fingerprints.txt` — the list naming this
  install's private values — blocks. Pattern-shaped findings — private-range addresses, home-directory paths, email
  addresses — stay advisory, because those patterns have real false positives and
  legitimate test fixtures carry them. If you genuinely mean to push a matching
  line (updating the fingerprint file itself is the usual reason), append
  `# privacy-override` to the push command; it says so out loud rather than
  passing quietly. An install with no fingerprint file, and any failure of the
  check itself, leaves pushes untouched.
