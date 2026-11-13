# spec-analyze: a read-only consistency pass over a spec

Checks a spec before a builder sees it. It finds a requirement stated twice
with different values, a reference to something no document defines, an
unmeasurable adjective where a number was owed, and a conflict with a rule the
spec does not get to override. Run it on the spec alone, or on the spec with
its plan and tickets when they exist.

Where it runs:
- `spec-session` runs it before a spec goes to a builder;
- `genesis-architect`, Step 0.8, runs it on the plan or spec it was handed.

What it does not do: it does not judge whether the change should exist, which
is the premise check (`.claude/docs/premise-check.md`), and it does not compare
a diff with its plan, which is the architect's Step 0.5. It checks the text of
the spec against itself, its companions and the standing rules.

## Inputs

- **Spec:** a path. Required. A spec held in an issue body is saved to a file
  first (`gh issue view <N> --json body --jq .body > ~/tmp/<N>-spec.md`) and
  saved again on every run, since an edit to the issue moves its lines. Line
  numbers cite that file; the `R-n` and `A-n` ids are the reference that
  survives an edit, so cite them first.
- **Plan and tickets:** paths, when they exist. Without tickets, the coverage
  pass is skipped and the report says so.
- **Standing rules**, always read:
  - the design principles and the Traps in the repo's `CLAUDE.md`;
  - the rules in `.claude/docs/premise-check.md`;
  - every ruling or recorded decision the spec itself cites, read at its
    source when you have access to it, and otherwise listed in the report as
    not read. An issue the spec cites is context, not a rule.

  A rule that its own source calls a guideline (premise-check.md's PR-shape
  bands, for example) is not a standing rule for this pass: a departure from
  it is HIGH, not CRITICAL.

**Read-only.** It writes nothing: not the spec, not the plan, not the tickets.
The report is its only output. Any fix is a separate edit the spec's author
makes after reading the report.

## Step 1: number the requirements

House specs carry no requirement ids, so assign them by these rules, applied
mechanically so that two runs on the same text agree:

- **One list item is one requirement**, however many sentences it has, when
  it states something the build must do, must not do, or must make true. A
  nested list item belongs to its parent.
- **Outside a list, each sentence is its own candidate.** It is a requirement
  when it contains, as a whole word and in any case, one of: must, shall,
  never, always, only; or the phrase "is kept" or "is stored"; or a number
  written in digits that the result has to meet ("30 days" counts; "one row"
  does not).
- **Acceptance items are numbered separately**, as `A-1`, `A-2`, … in document
  order: every item under a heading named Acceptance, Acceptance criteria,
  Tests or Done when. Such an item is an `A-n` only, never also an `R-n`.
- **Not requirements:** headings, rationale, examples, quotations,
  out-of-scope items, and a sentence that only points back to another
  requirement without adding to it ("the limit is the one set in step 2"). A
  sentence that restates another with a different value IS a requirement, so
  the duplication pass can see it.
- **A `## Spec analysis:` or `## Convergence` section and everything after it
  is a report, not the spec**, and is never numbered. So a report kept at the
  end of a spec does not change the numbers of a later run.

Number requirements `R-1`, `R-2`, … in document order, from the spec only.
Record the line each one starts on.

If the spec has no requirement at all, report that as one CRITICAL finding and
stop: there is nothing to check a build against.

## Step 2: the six passes

Run every pass. Cite a line for every finding.

1. **Duplication (`DUP`).** The same requirement stated twice. Twice with the
   same meaning is LOW (one should go). Twice with different values (a number,
   a limit, a name, a direction) is HIGH, and quote both.
2. **Ambiguity (`AMB`).** An adjective or adverb standing where a measurable
   value is owed: fast, robust, secure, scalable, minimal, reasonable,
   appropriate, soon, large, as needed, without a number or a test that
   decides it. An unresolved marker: `TODO`, `TBD`, `TKTK`, `???`, `XXX`,
   `<placeholder>`, `[fill in]`, an empty answer, or a sentence that stops.
3. **Underspecification (`UND`).** A reference to a step, file, function,
   table, setting, issue or document that none of the given documents defines
   and that does not exist where the reference says. A requirement with a verb
   but no object, no owner, or no way to tell whether it was met. An acceptance
   item that names no observable result.
4. **Standing-rule conflict (`RULE`).** A requirement or plan step that
   contradicts a standing rule from the Inputs list. Quote the requirement and
   the rule, with the rule's file and line or its id.
5. **Coverage (`COV`), only when tickets exist.** Both directions: a
   requirement no ticket delivers, and a ticket that serves no requirement.
   Map by the ticket's own reference to the requirement first, then by its
   content; say which.
6. **Inconsistency (`INC`).** One thing called by two names across the
   documents; an entity the plan names that the spec never does, or the
   reverse; an order that cannot work (a step that uses what a later step
   creates, with no stated dependency); two requirements that cannot both hold.

**The cross-document rule.** Before reporting anything as undefined (`UND`) or
missing from the spec (`INC`), look for it in every document you were given:
the spec, the plan, the tickets, and the repo when the reference is a path.
Something defined in a companion document is not undefined. A finding that
skips this check is wrong more often than one that does not: the first hand run
of these passes on a real spec reported an item as undefined that a second
document defined.

## Step 3: severity

- **CRITICAL:** a standing-rule conflict (`RULE`), always, except a departure
  from a rule its own source calls a guideline, which is HIGH (Inputs). **It
  cannot be downgraded**, whatever the spec argues, and it is fixed in the spec, not
  accepted. If the rule itself should change, that is a separate decision by
  the owner, outside this pass. Also CRITICAL: a spec with no requirements, or
  a requirement no ticket covers when it is the change's main purpose.
- **HIGH:** a duplicate with different values; two requirements that cannot
  both hold; an undefined reference a builder needs in order to build; an
  acceptance item no one could check; an unmeasurable word on a safety,
  security, privacy or performance requirement.
- **MEDIUM:** other ambiguity and underspecification; a coverage gap on a
  secondary requirement; terminology drift that could send a builder to the
  wrong thing.
- **LOW:** a same-meaning duplicate; wording; drift that cannot mislead.

## Step 4: ids that survive a re-run

A finding's id is its pass code and its anchor: the requirement or acceptance
item it concerns (`AMB-R4`, `UND-A2`); for a finding that involves several (a
duplicate, a conflict, two names for one thing), the LOWEST-numbered of them,
R before A, so a duplicate of R-4 and R-9 is `DUP-R4` whichever you noticed
first; for a finding that concerns neither, the line in the spec where it
starts (`UND-L37`). A second finding with the same id gets
`.2`, `.3` in line order (`AMB-R4.2`). A finding in the plan or tickets
anchors to `P-L<n>` or `T-L<n>`. Run twice on unchanged text, the ids are the
same; they are never numbered by the order you found them in.

## Step 5: the report

```
## Spec analysis: <spec file name>
Inputs: spec <path>; plan <path | none>; tickets <path | none>
Standing rules read: CLAUDE.md, premise-check.md, <cited ids read>; not read: <ids, or none>
Requirements: <n> (R-1 to R-n); acceptance items: <n> (A-1 to A-n)

| Id | Pass | Severity | Location | Summary | Recommendation |
|---|---|---|---|---|---|
| RULE-R7 | RULE | CRITICAL | spec:L52 | <what, quoting the requirement and the rule> | <the change to the spec> |

Coverage (tickets only):
| Requirement | Ticket(s) | Notes |
|---|---|---|

Counts: CRITICAL <n>, HIGH <n>, MEDIUM <n>, LOW <n>; by pass DUP <n> AMB <n> UND <n> RULE <n> COV <n|skipped> INC <n>
Dropped: <n> findings over the cap (<severity of the lowest kept>)
```

Order the table by severity, then by id. At most 50 rows: when there are more,
drop the lowest severity first, and within one severity drop from the last row
in that order upward; give the count dropped. With no findings,
say so in one line and still give the counts and the requirement total; an
empty report and a skipped pass must not look alike.

Every recommendation is an edit to the spec, the plan or the tickets that the
author can make. A finding you cannot quote a line for is not reported.

## What the caller does with it

The report goes with the spec. When `spec-session` hands a spec to a builder,
every CRITICAL finding is fixed and the pass run again; every HIGH finding is
fixed or carried with a reason; the final report travels with the handoff
(see `spec-session`, which keeps the report in a file beside the spec). An
architect review puts the CRITICAL findings on its own severity ladder
(`genesis-architect`, Step 0.8).

Adapted from `templates/commands/analyze.md` in github/spec-kit, tag v1.4.0.
Copyright GitHub, Inc. MIT License; the full notice is in
`LICENSE-spec-kit.md` beside this file.
