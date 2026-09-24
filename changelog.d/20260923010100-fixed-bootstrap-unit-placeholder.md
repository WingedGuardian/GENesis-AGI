- **`bootstrap.sh` wrote a broken `agent-zero.service`, and nothing noticed.**
  Unit files are generated from templates containing `__TOKEN__` placeholders,
  and two scripts generate them: `install.sh` and `bootstrap.sh`. Each kept its
  own hand-written list of substitutions, and the lists had drifted —
  `install.sh` learned to substitute `__AZ_ROOT__` and `bootstrap.sh` never did.

  `sed` does not complain about a token it has no instruction for; it copies it
  through. So `bootstrap.sh` produced a unit reading
  `WorkingDirectory=__AZ_ROOT__`, which installs and enables reporting success
  and could only ever fail at start. MEASURED on a live install: that exact line,
  with a correctly substituted path on the line directly below it.

  Both scripts now substitute the same set, and a test pins them to each other
  and to the tokens the templates actually use. The test asks "is every token
  used by a template handled by every generator" rather than looking for tokens
  known to be bad, so the next template that invents one fails immediately
  instead of shipping a unit with a literal placeholder in it.

  In practice this was latent for most installs, since `agent-zero.service` is
  not enabled by default — but the same class had already reached a unit once
  before, and the generators had no way to catch the third occurrence.

  **Nothing to do on your side.** The next time you run `bootstrap.sh` the unit
  is rewritten correctly.
