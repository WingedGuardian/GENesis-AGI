- **A malformed tool call no longer reports itself as a missing value.** When a long
  free-text argument is sent with a closing tag that does not match its opening tag,
  every parameter declared after it is absorbed into that string, and the server then
  reports those parameters as *missing* — which reads like the caller forgot them, or
  like a broken tool. One such call was refused six times in a row before the shape was
  recognised, with the evidence sitting in the error's own echoed input the whole time.
  The MCP middleware now inspects a missing-argument error, and when a *provided*
  argument's text contains the markup for a parameter reported *missing*, it replaces
  the message with both readings — the malformed-call one and the genuinely-absent one
  — and what to do in each case. It is offered as a possibility rather than a verdict,
  because prose *about* tool-call syntax looks identical from the arguments alone. This
  runs on every MCP tool rather than the handful that happen to have that shape, and
  only ever on a call that is already failing — it never alters arguments and cannot
  make a well-formed call fail.
