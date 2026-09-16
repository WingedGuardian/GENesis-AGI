- **The deploy advisory no longer recommends the one mechanism that has killed
  deploys, and no longer goes silent when you use it.** It told every session to
  run `update.sh` in the background, and then said nothing when one did — so it
  was quiet in exactly the case it exists to prevent. Deploys started that way
  have been killed mid-run twice, once leaving the server down through bootstrap.

  Both surfaces now prescribe a detached `systemd-run --user` launch you can paste
  verbatim, with the `--working-directory` and `--setenv=PATH` that a `--user` unit
  needs and an `is-active` check afterwards — a launch that fails is otherwise
  indistinguishable from one that worked, because `systemd-run` does not inherit
  your shell's directory and `--collect` removes the evidence. `--scope` is called
  out as not a substitute: it isolates the cgroup but keeps the caller's session,
  so it does not detach.

  Two things measured while fixing it, both of which had been stated the other way
  in our own notes. Backgrounding has no ten-minute ceiling — a 400-second task
  completed cleanly — so the problem was never a timeout; it is that a background
  task is bound to the session. And a foreground deploy killed at the ceiling does
  not stop halfway: `update.sh` traps the signal and rolls back, unwinding a deploy
  that was going fine. That second claim had been true when it was first written
  and was made false thirteen hours before the advisory carrying it shipped, by a
  change to the very file it described.

  The guard now fires on any deploy that has not left the session and stays quiet
  once it has, and it decides that by recognising the forms that *do* detach rather
  than by listing ones that do not — an earlier attempt blacklisted `--scope` and
  was walked past by `--scop`, `--sco` and `--sc`, all of which systemd accepts. It
  also looks at the part of a compound command the deploy is actually in, so a
  detached launcher somewhere else on the line no longer vouches for it.

  The invariant is held by tests that run the guard rather than read it, because
  every wording check passed while the guard sat silent on the dangerous input.
