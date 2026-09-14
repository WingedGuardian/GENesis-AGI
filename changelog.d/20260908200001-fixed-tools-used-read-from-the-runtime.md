- **The tools a session used are now read from the session, not from its own
  reply.** The same self-review pipeline told its graders which tools an
  interaction used by pattern-matching the reply's text — so a reply that merely
  *discussed* running a tool was reported as having run it. Genesis now records
  tool use as it happens and reports that instead. Where it cannot — some
  invocations do not stream — the graders are told plainly that any names were
  extracted from the reply and may be tools it only mentioned, and where there
  is no evidence either way they are told that too, rather than being handed a
  bare "none" that reads like a finding. One visible consequence: short
  interactions that genuinely used a tool are now reviewed where some were
  previously filtered out, so review volume rises slightly.
