- **A fail-open in a local development guard is no longer withheld from the
  public tracker as though it were a security defect.** The filing rule said a
  security defect — *"an unpatched bypass, a credential exposure, anything
  exploitable"* — is never filed publicly before it is fixed. That is right, but
  the bare word "bypass" swept in every guard fail-open, which is most of what
  this repo builds. The rule now turns on **who gains**: a defect is
  security-class when disclosure hands someone a capability they do not already
  have. A local hook fails that test on its own terms — it runs only where an
  install wires it, whoever can trigger it already has commit access to that
  checkout, and the worst outcome is an under-reviewed change reaching a pull
  request that still waits on maintainer approval, which is the state of any PR
  authored without those hooks.

  The rule also now says to verify the chain rather than the wording. The
  specific misfire it was written from: a deny message naming *"secrets"* was
  read as proof that credential scanning was the defeated control. It was not —
  that message describes what `--no-verify` does to git's own pre-commit chain,
  while this repo's privacy controls are a blocking CI job and two PreToolUse
  hooks, none of which that flag reaches.

- **The genesis-development skill now carries the prior that decides whether a
  guard should refuse at all**, ahead of its existing section on which direction
  a guard should fail in. Advisory is the default; escalating to a block needs a
  specific, credible, measured reason, and neither "for safety" nor a single
  incident is one. The section is explicit that this answers the VERDICT
  question — advisory or block, when the guard evaluates — and not the DEGRADE
  question of what a guard does when it cannot evaluate, which the existing
  fail-semantics section decides per boundary and whose contract test requires
  many hooks to fail closed. It also scopes itself to guards rather than
  approval boundaries, described as a class (anything gating autonomy,
  spending, data destruction, or publication) rather than as a list of names.
  An adversarial audit of the first draft found it stating all three of those
  too broadly — text that would have contradicted an enforced test, banned a
  shipped ask pattern, and read as a four-member denylist — which is recorded
  here because the audit requirement on prompt surfaces is what caught it.
