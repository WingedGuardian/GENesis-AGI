---
name: spec-session
description: >
  This skill should be used when a session SPECIFIES a Genesis change before it
  is planned or built: shaping the problem, the PRD, the architecture and the
  tickets a build session will take. Load `genesis-development` (the shared core)
  first. Do NOT load it for building (`build-session`), closing open PRs
  (`closing-session`), or post-merge verification (`validating-merges`).
# MEASURED against the real scorer on the live catalog, 2026-10-21: 4/4
# advertised spec prompts fire this skill, 0/10 control prompts do.
keywords: [spec, specs, prd]
consumer: cc_foreground
phase: 10
skill_type: workflow
---

# Spec session

**Load `genesis-development` (the shared core) first** if this session has not:
it holds the review-chain, evidence and private-data rules every session uses.

A minimal skill for now; its process is designed later. Also read:
- the core's "Due diligence before building" and "Architecture Review"
  (`.claude/skills/genesis-development/SKILL.md`);
- `.claude/skills/genesis-development/references/due-diligence.md` and
  `.claude/skills/genesis-development/references/plan-docs.md`;
- `.claude/docs/premise-check.md`, and the `evaluate` skill for any external
  candidate;
- `build-session`'s "Generalizability Gate" before a spec names where code runs.

### Plan documents carry a structured header

A plan doc that outlives one session (`~/.claude/plans/<name>.md`) opens with
YAML frontmatter naming what it commits to and what it was written against:

```yaml
---
plan: <slug>          # matches the filename
status: active        # active | stalled | superseded | done
updated: 2026-09-15   # last revision of the LIVE section
pinned:
  main: <12-hex>      # origin/main as of the `updated` date above
binds: "<one line — what adopting this plan commits us to>"
prevents: "<one line — what it forecloses, or nothing>"
---
```

Quote `binds` and `prevents` — they are prose, and unquoted, a `": "` makes the
whole header unparseable while a `" #"` **silently truncates** the value
(`binds: PR #2046 ships first` loads as `"PR"`).

Optional `decisions:` / `ledger:` / `issues:` lists name the trackers this plan
executes, in FULL ids, so a reader of either end can find the other. An absent
list means "none" — so only omit it once you have looked; write
`issues: unchecked` if you have not. In the body,
`## ═══ SUPERSEDED BELOW ═══` divides live content from archaeology; no divider
means the whole file is live. What the body must STATE is set by "Due diligence
before building" in the core, for every development plan presented for approval.

**`pinned.main` is the field that pays for itself**: it makes
`git fetch origin main --quiet && git log --oneline <pinned.main>..origin/main`
a one-command staleness read, on a document whose line numbers and PR heads
otherwise go quietly false. The fetch is load-bearing — against an unfetched
`origin/main` the command prints nothing and reads as "no drift". A moved main
means re-verify, never that the plan is wrong.

Nothing checks any of this yet — the header is written by hand, and a plan
missing it fails silently. Rationale field-by-field, what the divider does NOT
do for a grepper, and what was and was not carried across from the upstream
schema: `.claude/skills/genesis-development/references/plan-docs.md`.

### Auditing Existing Capabilities — enumerate, don't spot-check

Before claiming Genesis "lacks X", "needs to add X", or is "weaker than
<external system> at X" — or before any competitive/architecture comparison —
verify by ENUMERATION, not a spot-check. **Auditing a symbol is not auditing the
stack**, and a negative from a positive search is not evidence of absence:

1. Enumerate the subsystem's full module inventory before concluding anything is absent.
2. Trace the call graph BOTH directions — mechanisms often live in the
   wrapper/caller layer, not the first symbol (CRAG lives in the MCP recall
   wrapper, not `retrieval.py`; the reranker is applied by the caller).
3. Grep by CONCEPT with several synonyms, not one symbol.
4. Verify built/enabled/disabled against RUNTIME state (env gates, server logs),
   not code presence.
5. Multi-path systems → coverage matrix (N entry points × M mechanisms); hot
   auto-fired paths often carry a thinner stack than the deep path — a gradient,
   not an absence.
6. Confidence is capped by enumeration completeness.

A 2026-06-30 competitive audit wrongly claimed Genesis lacked CRAG,
scope-before-rank, and a live reranker — all three had already shipped. Full
protocol: procedure `codebase_audit` / CC memory `audit-enumerate-not-spotcheck`.
For "does Genesis already have X", consult the subsystem map
(`docs/architecture/CURRENT.md`, via the `subsystem-map` skill) FIRST;
`.claude/skills/genesis-development/references/codebase-map.md` stays the package-level structural companion.

## Never reinvent what GitHub already does natively

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
