# Gate-fix lane

Applies to any PR whose diff touches the enforcement-hook surface (`scripts/review_budget.py` `HOOK_SURFACE_PREFIXES` / `HOOK_SURFACE_FILES`).

- **GATE-FIX LANE — a hook-surface PR gets two standing discovery rounds
  (standing user rule, 2026-09-09).** The shared evaluator now enforces the distinction
  mechanically. After exactly two rounds, a current unreviewed head may receive
  ONE confirmation request without another approval only when the request carries
  `<!-- genesis-review-request head=<full-40-hex> kind=confirmation -->`. The marker is
  dispatch evidence, never approval or review evidence. Once present, a repeated request
  needs fresh approval. A review after that confirmation, or any further discovery
  request, also needs fresh approval. Because a CLEAN confirmation adds no round, the
  marker itself is the one-shot token: once a marker exists for any head other than the
  current one, the confirmation is SPENT and every later request and fix commit asks. A
  head that drew findings itself is a round, never a fix to confirm, so it gets no free
  request. (Residual, by owner ruling: an approved UNMARKED request that comes back
  clean leaves the marked one unspent.)
  Past the limit both gates ASK the owner at the action and REFUSE in a
  dispatched session: the push guard for a further review request
  (`review_budget.py` `approval_required`; `git_push_guard.py`, the
  `_is_dispatched()` check after `_check_codex_round_escalation`), and the commit
  gate for a fix commit once a third round is recorded, or at two rounds once
  the confirmation is spent (`commit_approval_required`;
  `review_enforcement_commit.py` `_allow`). Once the local streak holds three
  consecutive defect-bearing external rounds, the escalation cap refuses that
  commit before `_allow` can ask (the local-streak table in closing-session, "Escalation cap"; `# escalation-ack`
  records a fresh decision). Unreadable round evidence takes the
  same ask/deny path at any count, with one install-local exception: with
  `hooks.asks.review_request: off`, a transient read failure on a lone review
  request becomes a note instead of an ask (a dispatched session is still
  refused; the commit gate still asks). The round-2 merge-or-abandon decision
  (rule 4 below) is doctrine; no gate makes it.

  **Scope: any PR whose diff touches the enforcement-hook surface** —
  `HOOK_SURFACE_PREFIXES` + `HOOK_SURFACE_FILES` in `scripts/review_budget.py`.
  Read those constants; do not copy them here — a hand-copied list is wrong the moment
  a hook is wired. `TestWiredHooksFenceGuardrail` keeps the WIRED-HOOK half
  self-maintaining FOR THE SPELLINGS IT RECOGNISES, by parsing
  `.claude/settings.json` (measured 2026-09-10: **54** wired command entries, **49**
  of them matched, **5** unmatched, yielding **42** unique paths — an earlier draft
  said "42 of 54", which divides a path count by an entry count and so implies twelve
  misses where there are five. The five are `.claude/hooks/*.sh` and inline blobs
  already covered by the prefixes, so no live gap — but a `python3 -u scripts/foo.py`
  or `node …` wiring would slip past it); it cannot do the same for the tracked CONFIGS the
  hooks read, because settings.json lists hooks and not the files they consume. Those
  entries are maintained by hand, so adding a config a hook's behaviour depends on means
  adding it to `_HOOK_SURFACE_FILES` deliberately (Codex P2, #1903). This lane is a STRICTER-REVIEW, FEWER-ROUNDS trade on that surface. It does
  not apply anywhere else, and applying it to an ordinary PR doubles review spend for
  nothing. The test is MECHANICAL and deliberately wider than the rationale — the
  surface includes hook wiring and the tracked configs the hooks read, so a PR that only
  incidentally edits one is still in the lane. That is the intended trade: a mechanical
  test nobody has to adjudicate beats a judgement call about whether an edit "really"
  changes the gate. Throughout this block "gate PR" means exactly "hook-surface PR".

  1. **Round 1 is DUAL and immediate.** CONFIRM Codex has fired (it auto-reviews when
     the PR opens). `--check-pr <N>` shows whether a review EXISTS at this head; it
     CANNOT distinguish "never requested" from "still running", so if you did not
     request one yourself, request one rather than reading a blank line as proof
     nobody did. Then
     run the secondary reviewer (`merge_gate.secondary_reviewer`) alongside it — two
     sets of blind spots in one calendar round, which is the scarce resource here. No
     secondary configured → Codex-only, and say so in the PR. Nothing validates that
     key, so a TYPO looks exactly like "absent" — read the value before concluding
     there is none, or the lane quietly runs at half strength while the PR truthfully
     reports Codex-only.
     **This lane carries STANDING approval for the secondary, and it is the ONE place
     that overrides the per-use rule (genesis-development, "The review chain").** The general rule — an alternate reviewer
     needs explicit per-use approval, and only when Codex is UNAVAILABLE — still governs
     everywhere else; here the secondary runs alongside a healthy Codex, at PR open,
     without asking (owner decision, 2026-09-09). Do not generalise this to any other PR.
     **One head = one round on every counter, however many reviewers saw it**; write the
     local mark ONCE with combined evidence. That single mark is `--defects` if EITHER
     reviewer raised a new BLOCKER/SHOULD-FIX/P1/P2 — a clean secondary does not launder
     a defect-bearing Codex round.
     **Two separate marks do NOT halve the budget; they can DISARM it, which is worse.**
     An earlier draft said "halves", and the code says otherwise: `bump_review_round`
     increments only on a DISTINCT staged hash, so a second defect mark on the same diff
     is idempotent and costs nothing (`review_state.py:850-865`). But `--clean` resets
     the streak to 0 **regardless of whether the staged diff changed** (`:780-784`), and
     an idempotent same-hash `--defects` cannot restore the round it just erased. So on
     one staged diff, in EITHER order, a mixed clean/defect pair records as CLEAN — the
     defect-bearing round vanishes and the cap it was arming goes back to zero. That is
     the failure the one-mark rule prevents (Codex P2, #1903).
  2. **Triage before touching code — and here it OUTRANKS the cheap-fix habit.**
     That rule (closing-session, "'Not blocking' describes the GATE, not the finding", and right for ordinary PRs) weighs what a fix costs before the owner
     decides. On this surface the cost is not the edit, it is the PUSH: it
     invalidates the review, and a push whose DELTA touches the hook surface is never
     classed review-trivial (`_classify_post_review_delta` tests the DELTA's files, not
     the PR's), so a one-line courtesy fix to guard code buys a whole extra review
     cycle. A push of ORDINARY PROSE can still ride the existing
     review — that is the one cheap fix this rule does not charge you for, and note
     how narrow it is: `_is_docs_or_config` DELIBERATELY excludes prompt surfaces
     even when they are `.md` (`review_enforcement_commit.py:114-127`), so a skill,
     command or agent file is SUBSTANTIAL and buys the full cycle
     (`review_scope.classify_compare_substantiality`). Since a gate PR's docs are
     usually exactly those files, assume you are paying (Codex P2, #1903). A cheap NON-floor finding gets a reply,
     not a commit. Merge both reviewers' findings into ONE table,
     then classify each: {live bug | latent trap | hardening | observation}. Only live
     bugs and cheaper-now-than-later traps change code. The rest get a maintainer reply
     carrying the evidence — IN-THREAD on the inline comment, from a maintainer
     account: that is the ONLY shape the gate scores as engagement (it reads
     `in_reply_to_id` on `pulls/N/comments`, so a top-level PR comment engages
     nothing). Two findings therefore have no thread to reply into and must be
     documented in the PR body instead: anything the SECONDARY reviewer raised, which
     runs locally, and anything living in a review BODY rather than an inline comment.
     Engagement costs ZERO rounds. Most findings never needed to become a diff.
  3. **One batched fix push per round, self-audited first.** Not two, not three: every
     push invalidates the review, so three pushes turn one round into three review
     requests. (A compliant run has at most two CODE pushes total — the free
     ordinary-prose push of rule 2 does not count against it — one answering round 1,
     and rule 4's terminal push if the floor demands one.)
     Before pushing, run the adversarial pass over your own FIX-CODE — reviewing your
     own work needs no approval and is the specific gap that keeps biting. READ, three
     instances (#1856, #1686, e7ae445a5): each a fail-open introduced BY fix-code
     written under review pressure, and each caught only by a later round. The danger is the fixing, not the
     reviewing.
  4. **Round 2 is the discovery stop**, and a terminal round is the owner's call
     (closing-session, step 4). The two branches are: accept
     and merge with every outstanding finding documented in the PR body, or abandon and
     re-cut from a design that does not need a third round. **The always-fix floor is
     never accepted** — a P1, a security defect, anything destructive or fail-open gets
     FIXED in the terminal push. "Accept and document" covers everything that is NOT
     floor-class, so a round 2 carrying floor findings has only two branches: fix them,
     or abandon.
     **A terminal push whose delta touches the hook surface needs a fresh review at that
     head** — that delta is never review-trivial, by design (a docs-only terminal push
     is the exception, and may ride the existing review — ORDINARY PROSE only, the
     same narrowness rule 2 spells out: a skill, command or agent file is a PROMPT
     SURFACE, classifies SUBSTANTIAL, and needs the fresh review even though it is
     `.md`. That is the identical claim rule 2 already corrects, and it survived here
     for a round because the fix was applied to the sentence that was flagged instead
     of to every sentence making the claim). That confirming review
     is a GATE REQUIREMENT, not a discovery round: if it is clean, the PR goes to the
     owner's merge decision (closing-session, step 4). If it
     surfaces something new, the SEVERITY routes it, exactly as it does anywhere
     else in this rule — a non-floor finding is accepted and documented like any
     other, and only a FLOOR-CLASS one (P1, security, destructive, fail-open) goes
     to the owner under rule 5. An earlier draft sent everything to rule 5, which
     contradicted the accept-and-document branch two sentences above and left one
     confirmation result with two required actions (Codex P2, #1903). A round 2 with NO
     floor findings needs no push at all, so there is nothing to re-review.
  5. **A NEW P1 on the fix-code at round 2 is an owner decision, not round 3.** That is
     the measured tripwire: a PR whose fix introduced a P1 is the kind that introduces
     another. Stop and ask. The P1 itself is NOT optional (rule 4's floor) — what the
     owner is deciding is fix-and-merge versus abandon-and-re-cut, never accept-and-ship.
     **With no user to ask** (a dispatched session): do not merge and do not open round
     3 — comment on the PR naming the tripwire and the P1, apply the
     `needs-architecture-session` label, move it to draft (`gh pr ready <N> --undo`),
     and open a `ready` follow-up (a dispatched session's follow-up lands in the `tabled` lane; the intake gap is tracked in #2857), the same
     unattended route closing-session, "Some PRs are not a review problem" uses.
  6. **After round 1 the diff SHRINKS in SCOPE, never grows.** Line count is not the
     measure — a floor fix under rule 4 may add lines and is mandatory. What may not
     grow is what the PR is FOR. A revert is fine; new capability is a new PR. This is what actually kills whack-a-mole — the loop is sustained by
     the diff expanding under review, not by the reviews themselves. (Composes with
     closing-session, "Keep the PR the PR": adjacent findings become issues.) Against
     enumerate-the-CLASS (closing-session, "A review's findings are a SAMPLE, not a to-do list"): fixing every instance of ONE defect inside the files
     this PR already changes is the same fix, not growth — that is the shape the class
     rule asks for. Reaching into files the PR does not touch, or adding capability to
     make a class fixable, is growth: that is an issue or a follow-on PR. Ask "Keep the
     PR the PR" FIRST — a PRE-EXISTING sibling instance the PR works fine without is an
     issue even when it sits in a file you already touch; this rule governs the reach of
     a fix you have already decided to make.

  **The machine implements this lane.** Gate-surface classification comes from the
  shared `review_budget` path predicate, including rename sources and destinations.
  The second-round fix is permitted, and the exact-head marked confirmation is the one
  exempt request. If that confirmation finds another defect and a further fix is needed,
  the commit requires fresh native approval. The independent local streak may still
  demand its round-2 class audit or round-3 stop; those are process interventions rather
  than substitutes for the per-action user decision.

  **Queue priority:** gate PRs get reviewed and driven before ordinary ones — read this
  as a THIRD standing justification alongside the two in closing-session, "When to DRIVE a Merge",
  granted for the same reason those two are: a gate fix is what unblocks the PRs stacked
  behind it, so leaving one to bake is the queue declining to repair itself.

  **On the word "round" here:** one head that drew findings is one round however many
  reviewers saw it. The shared evaluator deduplicates reviewers by commit identity, so
  a dual round does not spend two rounds.

  **A note for anyone reading only CLAUDE.md:** the shared evaluator now gates a third
  discovery request mechanically. The local round-2 MODE-SWITCH still acts on commits
  and remains a separate class-audit intervention.
