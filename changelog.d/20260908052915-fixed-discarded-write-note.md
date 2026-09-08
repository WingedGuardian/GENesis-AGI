- **A refused command now says the WHOLE command was discarded, not just the step
  it named.** A PreToolUse block cancels the entire Bash call, not the step the
  guard objected to — so `cat > config.py <<'EOF' … EOF && git commit`, refused for
  the commit, also loses the write, and the message mentioned only the commit. It
  read as "the commit didn't happen" rather than "and the edit you just made never
  happened". Measured on one box: of 722 multi-segment Bash calls a hook actually
  blocked, 288 carried a write nobody was told about. Every guard now emits a
  footnote at its own refusal point (`scripts/hooks/discarded_write.py`) — the guard
  about to refuse is the only thing that knows a block is happening, so no separate
  predictor exists to drift out of sync with the guards it models. Approval prompts
  carry the same warning in the dialog itself, since declining also skips every
  other step in the command, which is exactly what a "block the push?" prompt hides.
  The note names nothing: it states the one fact true of every block. An earlier
  design worked out WHICH files were lost, and naming a file means mapping argv to
  effect — which of `sed`'s spellings mean in-place, which operands are the program
  — a set with no closed boundary that produced fourteen review findings, nearly all
  of them one more spelling. It was also quadratic in the step count, which made a
  cosmetic helper into a fail-OPEN: a long command cost enough time to exceed the
  shell hook's registration timeout, and a hook killed before its `exit 2` lets the
  refused command run. The reader has their own command on screen, which is what
  they must re-read before re-running it anyway. Strictly cosmetic and unable to
  change a verdict: every import is guarded, its own work is bounded by a wall
  clock rather than by proxies for cost, and the shell hook asserts its exit code
  unchanged in both directions with the helper made deliberately unreachable. Three
  tests derive the consumer set from the code rather than a hand-written list —
  every guarded import matches its fallback exactly, every consumer that imports
  the helper also emits it, and every guard with an approval prompt appends the
  warning — because a rule enforced at six call sites is a rule the seventh will
  not have.
