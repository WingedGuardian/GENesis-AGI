# Guard failure regimes

**SYSTEMIC failure means no usable predicate survives**, so any refusal would be
TOTAL and the repair path closes with everything else — typically because the
machinery that runs guards is dead (the wrapper, the interpreter, the venv), or
because one missing dependency hits every guard at once. **There the default is
open-loud** (standing owner ruling, 2026-09-15). Genesis runs headless: the
interactive session IS the repair path, and the host Guardian is the
repair-of-the-repair. A guard layer that fails closed at that level does not
protect the machine, it bricks it, with nobody at a console to type the fix. A
messy action Genesis can clean up afterwards beats a system that can no longer
act at all.

Note what does NOT decide this: "how bad was the cause". A single missing
dependency produces EITHER regime, and the same helper contains one of each —
which is the clearest worked example in the repo, so apply the test to both:

- **`degraded_exit`'s predicate-gated leg** — a shared sibling (`shell_parse`)
  is unimportable, so the guard refuses only what its degraded MATCHER names.
  Read/Edit survive, `git checkout` is unmatched, the rates in build-session ("Guard failure semantics") are what it
  costs. Repair path open ⇒ **PARTIAL**, and fail-closed is licensed.
- **`degraded_exit`'s unconditional leg** — `hook_input` ITSELF is unimportable
  or version-skewed, so nothing the guard could import can recover it and each
  guard `os._exit(2)`s with no payload read and no predicate consulted.
  **The size of that refusal is no longer written down here, and that is the
  point.** It is DERIVED on every test run by
  `tests/test_hooks/test_import_time_degraded.py::test_every_bash_hook_declares_its_degrade_direction`,
  which parses `.claude/settings.json` (never greps it), runs every hook whose
  matcher can fire on Bash against a poisoned `hook_input`, and asserts the RULE
  the number was only ever evidence for:

  > A hook that can fire on Bash either carries a degraded handler — emits `GUARD
  > DEGRADED`, exits 2 — or is NAMED: in `_ADVISORY_BY_DESIGN` with words verified
  > verbatim against its own docstring, or in `_NOT_PYTHON_ON_BASH` because it
  > never imports the module. There is no third bucket.

  Polarity is ALLOWLIST, so a guard wired next year that forgets its handler fails
  by construction instead of quietly ceasing to guard. That matters because the
  omission is invisible in production: Claude Code treats a non-2 exit as a
  non-blocking error **when the hook emits no `permissionDecision`**, so a guard
  that dies on its import traceback exits 1, the command runs, and nothing
  distinguishes it from a guard that looked and approved. (The qualifier is not
  pedantry: an exit-0 hook carrying `permissionDecision: "ask"` does gate, and
  this repo has one — so "non-2 means non-blocking" is true of the degraded leg,
  where that channel is unreachable, and not of hooks in general.)

  **THE POPULATION FILTER IS WHERE THIS KIND OF GATE FAILS, not the assertion**,
  and the first version of this one proved it. It compared `matcher != "Bash"` —
  an exact-string test against a field Claude Code treats as a REGEX — and
  discarded any command its narrow pattern could not parse. An adversarial audit
  broke it four ways against mutated copies of the real settings: a hook wired
  `"Bash|Edit"` was invisible, `".*"` was invisible, and an EXISTING blocker
  respelled as a bare `python3 …/guard.py` silently left the population with the
  suite still green. `procedure_advisor.py` is wired on `".*"` in this repo, so
  that was a live gap, not a future one. An enumerator that silently drops what it
  cannot parse is a DENYLIST one level up. The fix is the shape
  `test_hook_output_contract.py::_resolve` already uses: evaluate the matcher as a
  regex, and make an unresolvable command a FAILING row rather than a skipped one.

  Two limits remain, stated rather than assumed away: the enumeration is scoped to
  the REPO settings, so a user-level `~/.claude/settings.json` can wire more on the
  same matcher invisibly (MEASURED on one install: it wires a shell hook on
  `Grep|Glob|Bash`); and shell hooks on the matcher never import `hook_input`, so
  they are exempted BY NAME rather than left unseen.

  **Why a count became a rule — four failures, and only three were mistakes.**
  `grep degraded_exit(` finds six callers, and an early draft therefore said "six
  guards"; but this leg is precisely the one where `degraded_exit` is UNREACHABLE,
  so every refusing guard refuses from its own import handler and grep sees only
  those that ALSO call it on the other leg. Count the condition, never the helper.
  Correcting it to nine then left the denominator silently narrowed to the subset
  actually executed — **a figure written as "N of M things wired on X" has to
  re-enumerate X's full population programmatically or it ships one unstated
  restriction copied verbatim into every surface that quotes it.** Naming what M
  excludes is the fix; adjusting M is not.

  And then the fourth: the corrected figure went stale within a day with nobody
  being wrong at all, because another PR wired one more hook on the same matcher.
  A denominator maintained by hand in four places is not a measurement, it is four
  chances to be out of date — which is why the rule is asserted and the number is
  computed. **Two limits stay stated rather than assumed away:** the enumeration is
  scoped to the REPO settings, so a user-level `~/.claude/settings.json` can wire
  more on the same matcher invisibly; and the matcher also carries shell hooks,
  which never import `hook_input` and so cannot be affected — one of which is
  itself a blocking guard.

  **Bash alone is not the question the test asks** — what matters is whether ANY
  route to repair survives, and Write/Edit is one.

**THIS TEST FOUND A REAL BRICK ON ITS FIRST APPLICATION, and the fix is the
worked example of what it is for.** `scripts/pretool_check.py` carried the same
unconditional block on `Write|Edit`, so the session could neither run a command
NOR edit a file: repair path CLOSED, SYSTEMIC, on a headless box. And that
refusal was a strict OVER-block rather than a conservative one — this guard
blocks CRITICAL-path writes in DISPATCHED sessions only, an interactive session
is allowed through by design, and its healthy session test
(`os.environ.get("GENESIS_CC_SESSION")`) needs nothing from `hook_input`. It was
refusing a category it was built never to refuse, and that category is the one
that performs repairs.

So the degraded block now asks the guard's own scope question with the one input
that cannot fail: a DISPATCHED session still refuses (nobody is present to
approve), an INTERACTIVE one is allowed with a loud notice. The security delta
is ZERO and is locked by a test rather than asserted — the healthy guard already
permits exactly that call, so restoring it surrenders no protection that existed.
Bash stays refused, which is the point: fail-closed keeps everything it was
protecting, and the session keeps the edit that repairs the tree.

**A DISPATCHED session remains SYSTEMIC by this same test** — Bash and Write/Edit
both refused, repair path closed — and that is the intended direction rather than
an oversight: an unattended session is not who should be self-repairing a broken
guard tree, and its repair route is a human interactive session, which is exactly
what this change makes usable again.

**The general rule, which is why this sits in the doctrine and not only in a
changelog:** when a fail-closed leg has no predicate, do not ask whether the
refusal is severe — ask which repair routes it closes, and leave one open on
purpose. Two other routes exist here and NEITHER is a design: an MCP editing
tool is matched by neither matcher, and the host Guardian can `REVERT_CODE` from
outside the container, but nothing currently WAKES it, because the container is
up and `genesis-server` never imports `hook_input`. A repair route that survives
by accident is not a repair route; name it and WIRE it.
