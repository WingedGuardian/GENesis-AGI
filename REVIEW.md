# Review rules

These rules are for any automated reviewer of this repository. The full review
mandate is the "Code Review Mandate" section of `AGENTS.md`. This file repeats the
one rule the merge gate depends on.

## Three kinds of finding are always at the blocking level

The merge gate's always-fix floor reads a finding's severity label: a Codex **P1**,
a CodeRabbit **Critical** or **Major**, or a Devin **severe** finding. It does not read
what the finding is. Label a finding at that level, whatever its size, when it shows
that the code:

- **leaks:** the change exposes data that is actually private (personal data, a secret,
  a credential) to an audience beyond the one it is meant for, such as a commit, a PR,
  an issue, a public log or a published file;
- **destroys:** it deletes or overwrites user data, or runs an irreversible operation
  nobody asked for, with no trash, backup or confirmation to recover from;
- **fails open:** a check whose job is to stop data loss, credential exposure, deletion
  or an unreviewed merge lets the action through when it errors, times out or cannot
  read its input.

Not every fail-open qualifies. A fail-open the code documents as deliberate (an advisory
hook that must never wedge a session, for example) and a deliberate multi-step evasion
of an accident-prevention guard are notes at most; the threat-model rule in `AGENTS.md`
governs them.

Put one line naming the kind, exactly `Kind: leak`, `Kind: destructive` or
`Kind: fail-open`, as the first line of the finding's description: after your own
severity header or badge, never ahead of it. The gate reads the label from the comment's
first line, so a line ahead of the header hides the label.
