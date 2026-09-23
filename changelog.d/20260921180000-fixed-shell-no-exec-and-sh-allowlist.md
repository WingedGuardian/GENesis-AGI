- **A shell invocation the shell never executes is no longer reported as a
  command, wherever the option sits.** The parser decided "does this run?" from
  the option bundle carrying `-c` alone, so every option BEFORE it was ignored:
  `bash -n -c CMD`, `bash -o noexec -c CMD` and an invalid `bash -z -c CMD` all
  parse and run nothing, yet each produced a nested segment and every guard
  downstream blocked a command that was never going to execute.

  Measured against the installed bash rather than reasoned about, and the
  measurement changed the fix. No-exec is not a property of the `-c` bundle —
  it is ONE left-to-right scan over the whole option list, and the two letters
  involved do not behave alike. `n` is ordinary shell state: `+n` clears it and
  the LAST occurrence wins, so `bash -n +n -c CMD` really does execute CMD and
  a naive "any `-n` means inert" rule would have hidden a live command — a
  bypass introduced by the fix for a false block. `D` is an invocation action
  rather than state: either sign selects it and nothing later clears it, which
  is why `bash -c +D CMD` was still reported. State now carries in both
  directions across the selector, so `bash -n -c +n CMD` (runs) and
  `bash -o noexec -c +o noexec CMD` (runs) are distinguished from
  `bash +n -n -c CMD` (inert). Twenty-one shapes are pinned by a corpus that
  re-derives every expectation from the real shell on each run, so it cannot
  quietly stop describing bash the way the option table once did.

- **`sh` now carries the union of both shells' option letters, closing a bypass
  on hosts where `/bin/sh` is Bash.** The allowlist is selected by the
  executable's BASENAME, not by the binary the name resolves to. Where `sh` is
  dash — as on the machine the table was measured on — dash's strict set looked
  right. Where `sh` is Bash, `sh -ch CMD` runs the command while the strict set
  rejected the bundle, so the parser returned no script at all and the nested
  command was invisible to every segment-based guard.

  The two error directions are not symmetric: too strict HIDES a command that
  really runs, while too permissive costs a false block on an invocation that
  fails anyway. `sh` therefore takes the union and `dash` keeps the strict set
  under its own name, where the basename does identify the binary. Recorded
  because the first attempt at this fix used bash's letters alone and dropped
  `I` and `V`, which dash accepts — reintroducing the same bypass in the
  opposite direction, on the very host it was being tested on. The existing
  dash-bundle tests caught it.

- **A shell-inertness test no longer passes when the shell it probes is
  missing.** The probe runs the inner interpreter through `bash -c` and asserts
  its payload did not print. With that interpreter absent, bash reports the
  failure on stderr and leaves stdout empty, which satisfies the assertion for
  entirely the wrong reason. The skip is now keyed on the template's own
  interpreter rather than on one shell by name, so a row added later for another
  shell cannot reintroduce it.
