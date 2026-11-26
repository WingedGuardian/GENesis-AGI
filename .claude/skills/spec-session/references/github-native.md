# Never reinvent what GitHub already does natively

**Standing user rule, 2026-09-09.** Before writing repo-local code that decides
something about a pull request, branch or merge, check whether GitHub already
enforces it: rulesets and required status checks, CODEOWNERS, review-thread
resolution, stale-review dismissal on push, path-based labelling, issue and PR
templates. If it does, use that — and only depart from it for a specific,
concrete reason GitHub cannot meet, stated where the code lives.

**Why this is a rule and not a preference.** Enforcement written here is a
script that every install must have wired, correctly, forever; enforcement
written as GitHub config is one versioned thing that binds identically for
everyone and cannot drift between installs. MEASURED on this repo: hook WIRING
had silently diverged between two installs in a security hook, which is
invisible to CI because the scripts are tracked and the wiring is not.
`git_push_guard.py` is 8,971 lines (measured 2026-09-10; it was 8,222 on 09-09 — a figure in a durable doc is a claim with a date, re-measure rather than quote), and the fraction of it that only reads GitHub
state is the fraction that never needed to be local.

**How the layers actually split** — this is a division of labour, not a
migration, and the third layer is not a consolation prize:

- **GitHub config** takes what is a FACT ABOUT GITHUB STATE: is this check
  green, is this branch behind, was this thread resolved, who owns this path.
- **A required check run** (issue #1670) takes composite judgements that still
  read only GitHub state — CI rollup identity, review freshness, finding
  scores. Port these one at a time, and DELETE each ported check from the local
  guard in the same PR. Two enforcers of one rule is replica drift with extra
  steps, and the copy nobody is watching is the one that goes wrong.
- **Local hooks** keep everything reading THIS BOX or shaping THIS SESSION:
  review evidence and markers under `~/.genesis/`, override sigils, the
  bash-safety and destructive-command guards, the round caps. These are not
  merge conditions at all — a PreToolUse guard runs before a command executes,
  which is a place GitHub has no presence. Round caps in particular: a research
  pass across production OSS review tooling found no implementation of them, so
  that machinery is ahead of the field rather than behind it.

**The check before you build:** name the GitHub feature you considered and why
it does not fit. "I did not think to look" is the failure this rule exists to
catch, and it is invisible afterwards — a hand-rolled implementation of a
platform feature looks exactly like a hand-rolled implementation of something
novel.
