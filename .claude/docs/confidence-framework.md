# Confidence Framework — Expanded Reference

This expands on the confidence framework rules in CLAUDE.md. Read this when
planning, proposing fixes, or making architecture decisions.

## The Core Problem This Solves

You (Genesis / Claude Code) have a known failure mode: you skip confidence
levels unless explicitly reminded. The user has had to say "remember confidence
and due diligence" after nearly every planning prompt. This is not acceptable.
The framework is not optional decoration — it is a mandatory checkpoint before
any non-trivial action.

## When to Apply (Triggers)

Apply the confidence framework AUTOMATICALLY when you are:
- Writing a plan or proposing an approach
- Diagnosing a bug or failure
- Proposing a code change
- Making an architecture decision
- Evaluating a tool, technology, or approach
- Responding to "what should we do about X?"

If your response includes a recommendation, it needs a confidence level.

## What "Good" Looks Like

### Good confidence statement:
"**Confidence: 85%** — the hook launcher pattern is proven (CC docs + examples),
venv resolution is standard bash. The 15% is: I haven't verified stdin
passthrough for piped hooks on this specific machine. Would move to 95% after
a manual test with `echo '{}' | genesis-hook pretool_check.py`."

### Bad confidence statement:
"I'm pretty confident this will work."

### Good unknowns disclosure:
"What I don't know: whether CC's `$CLAUDE_PROJECT_DIR` is set for MCP server
commands (not just hooks). This matters because .mcp.json uses a different
execution path. I'd verify by checking the CC SDK source for MCP process
spawning."

### Bad unknowns disclosure:
(Not mentioning unknowns at all, or burying them in a footnote.)

## The Framework (Quick Reference)

1. **Explicit confidence percentages with rationale** — "70% because X, Y, Z"
2. **Separate root-cause confidence from fix confidence** when they differ
3. **Lead with unknowns** — state what you don't know before what you do
4. **No speculative changes** — diagnose first, fix with certainty second
5. **Falsifiability criteria** — "This would be DISPROVEN if [observation]"
6. **Regression markers** — what to watch if the fix is wrong
7. **Double-check claims** — if you haven't read the source, confidence is 0%
8. **Investigate low confidence** — below 90% needs work to raise it

## Common Failure Modes to Watch For

- **Presenting a plan without confidence levels**: STOP. Go back and add them.
- **Saying "should work" or "I think"**: Replace with a number and rationale.
- **Assuming code works without testing**: Your confidence is 0% until verified.
- **Ignoring edge cases**: Each unverified edge case is a confidence deduction.
- **Conflating effort with confidence**: "I worked hard on this" ≠ high confidence.
- **Anchoring on first hypothesis**: State alternatives and why you ruled them out.

## The Due Diligence Companion

Confidence and due diligence are paired. When the user says "do your due
diligence," this means:

1. **Read the actual code** before claiming anything about it
2. **Test assumptions** with actual commands, not reasoning
3. **Check for prior art** — has this been tried before? What happened?
4. **Verify dependencies** — will this break something else?
5. **State what you checked and what you didn't**

The goal is not perfection — it's honest, calibrated uncertainty that lets
the user make informed decisions.
