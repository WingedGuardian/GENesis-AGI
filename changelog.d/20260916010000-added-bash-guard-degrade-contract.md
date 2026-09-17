- **Every hook that can fire on Bash must now declare its degrade direction, and
  the size of the refusal is computed rather than written down.** A new allowlist
  gate parses `.claude/settings.json`, runs each such hook against a poisoned
  `hook_input`, and asserts that it either carries a degraded handler (emits
  `GUARD DEGRADED`, exits 2) or is NAMED — in `_ADVISORY_BY_DESIGN` with words
  verified verbatim against its own docstring, or in `_NOT_PYTHON_ON_BASH` because
  it never imports the module. There is no third bucket, so a guard wired later
  that forgets its handler fails by construction instead of quietly ceasing to
  guard — an omission invisible in production, because Claude Code treats a non-2
  exit as a non-blocking error when the hook emits no `permissionDecision`, so a
  guard dying on its import traceback exits 1, the command runs, and nothing
  distinguishes it from a guard that looked and approved.

  Motivation: that figure was wrong three times in review and then went stale a
  fourth time with nobody being wrong at all, when another change wired one more
  Bash hook. Four hand-written copies existed across the repo; all four are now
  gone, replaced by a pointer to the derived gate.

  **The population filter, not the assertion, is where this kind of gate fails.**
  The first version compared `matcher != "Bash"` — an exact-string test against a
  field Claude Code treats as a regex — and discarded any command its narrow
  pattern could not parse. An adversarial audit broke it four ways against mutated
  copies of the real settings: alternation and wildcard matchers were invisible,
  and an existing blocker respelled as a bare `python3 …/guard.py` silently left
  the population with the suite still green. One in-repo hook was already outside
  the gate for that reason. The enumerator now evaluates the matcher as a regex
  and makes an unresolvable command a failing row rather than a skipped one, the
  shape `test_hook_output_contract.py::_resolve` already used. All four evasion
  spellings, a collapsed enumeration, and an invented allowlist justification were
  each confirmed to turn the suite red.

  The rule itself was derived by execution rather than assumed: the first
  hypothesis — that a hook importing `hook_input` blocks — was refuted, since all
  of them import it at module scope, including every non-blocker. A docstring
  keyword scan was tried as the discriminator and rejected, because "advisory"
  also appears in three hooks that do refuse. Two limits are stated rather than
  assumed away: the enumeration is scoped to the repo settings, so a user-level
  settings file can wire more on the same matcher invisibly, and shell hooks on
  the matcher never import the module and so are exempted by name rather than
  left unseen.
