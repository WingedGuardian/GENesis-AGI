- **The privacy check that runs before publishing now refuses, rather than
  advising.** It previously printed a warning and let the push proceed, which
  meant the decision rested on someone reading a message at the exact moment
  they were trying to finish something. It also now recognises credential shapes
  it had never been taught — a long random value assigned to a name that reads
  like a secret is caught on its shape, not on matching a known provider's
  format.
- **A comment can no longer vouch for the code beside it.** Any trailing text on
  a line was enough to make the check treat an inline credential as a harmless
  reference, so adding a note after an assignment quietly disarmed it. The check
  now looks only at what was actually assigned.
- **Publishing a branch and opening its pull request are now documented as one
  act.** A branch pushed with no pull request receives no automated checks at
  all, because those run on the default branch and on pull requests, and such a
  branch is neither — so the secret scan never looks at it. Two shortcuts that do
  not work are written down so they are not rediscovered: asking to open a pull
  request does not push for you when nothing is watching for a prompt, and
  joining the two commands together is refused on purpose, so that publishing is
  approved once per push rather than once per pair.
- **The gate that checks for an open pull request had an untrue assumption, and
  its test could not have caught it.** Both are corrected.
