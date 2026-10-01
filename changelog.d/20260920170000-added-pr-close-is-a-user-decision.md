- **The rule against retiring someone else's pull request is now reachable
  from the command line.** `genesis-development` carries a standing rule — a
  reviewer session retires nothing; a premise-wrong PR gets the
  `needs-architecture-session` label and stays open; a superseded one gets a
  comment naming its successor and also stays open; retiring belongs to the
  session taking up the revival. Nothing enforced or surfaced it, so it bound
  only a session that happened to have loaded the skill. A new advisory hook
  prints the rule at the moment a command would close a PR, on the forms
  visible in the command text: `gh pr close` (including the separated-repo
  spelling that was once a real bypass in the merge gate), a
  `closePullRequest` GraphQL mutation, and a `state=closed` field on a pull or
  issue endpoint. It never blocks, never prompts, and behaves identically in a
  dispatched session as in a foreground one.

  Advisory by design rather than by compromise. An enforcing version drew
  eleven review findings, five of them one unanswerable question: whether a
  gate can tell from argv that a command closes a PR. It cannot — `gh api
  graphql` takes its mutation from stdin or a file, neither of which is in
  argv. Under enforcement that gap is a bypass which must be closed and cannot
  be; under an advisory it is a note that did not fire. The note says so
  itself, so a reader meets the boundary where they rely on it rather than in
  a docstring.
