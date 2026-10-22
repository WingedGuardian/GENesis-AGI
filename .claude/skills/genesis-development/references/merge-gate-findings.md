# Merge-gate findings

   **Devin scores like Codex (owner ruling, 2026-09-24).** Its comments open
   with `<!-- devin-review-comment {json} -->` and a marker: red is Devin's severe
   tier (bug / critical security), yellow its non-severe tier (bug / security
   warning), and 🔍 is informational, surfaced and never scored. The marker agrees
   with the metadata `kind` on every comment measured. A
   marker outside that closed set is FORMAT DRIFT: surfaced, never scored. A
   finding Devin posted twice counts once (dedup on its metadata `id`). It is
   parsed only for the `devin-ai-integration[bot]` Bot account, and its branch
   runs before the Codex badge match, so a Devin body QUOTING `![P1 Badge]` is
   scored once, as Devin. CodeRabbit Minor and below stay SURFACE-ONLY by owner
   ruling — only Devin gained scoring.

   **Clearing a finding:** a MAINTAINER's in-thread reply, as always — or, for
   a DEVIN finding only, Devin itself replying in that finding's thread with a
   first line starting `✅ **Resolved**` (Devin may withdraw its own finding; no
   bot can clear another's, free prose such as "Fixed in …" clears nothing, and
   Codex and CodeRabbit findings still need a maintainer reply). Devin's reviewer
   and its builder post under ONE login, so a self-withdrawal is reply text, not
   an identity: it still clears, but the gate always LISTS it as
   `[Devin self-withdrawn]` so you can check the fix landed. A thread
   resolved in the GitHub UI does NOT clear a finding; reply instead. A finding
   Devin posted more than once counts once: each copy's diff and documentation
   scope is judged on its OWN anchor, the finding scores on its strongest copy
   that is eligible to score, and every copy carrying an unrecognised marker is
   reported as format drift, answered or not.

   **`auth` is deliberately NOT a critical input**, though it sits in
   `_DOMAIN_SENSITIVE_TAGS` and drives the depth gate. Its glob is
   `*auth* *session* …`, and MEASURED over 3,999 tracked files, 58 of the 59
   `auth`-tagged files (98%) match on **"session"** — CC-session machinery, not
   authentication. Exactly one is real (`dashboard/auth.py`). Re-inheriting that
   set is the obvious "cleanup"; don't.

   One thing the lane does not change: **a single CodeRabbit
   Critical or Major blocks on its own, but ONLY from the INLINE endpoint.**
   `_check_inline_review_findings` reads two channels and they are NOT symmetric:
   `pulls/N/comments` (findings anchored inline) feeds the score, while the review
   BODY — CodeRabbit's "Outside diff range comments", the findings it could not
   anchor — is surfaced loudly and scores **0.0 at every severity**. The
   `outside_critical` / `outside_major` accumulators appear in no term of
   `score = len(p1) + 0.5*len(p2) + 1.0*len(cr_block)`, deliberately: an
   undelivered finding has no comment thread, so there is no maintainer-reply
   route to clear it. Read them anyway; nothing there will stop a merge. The
   gate's own block message prints the whole formula; read it rather than
   reciting this.
   A finding is excluded from the score when a MAINTAINER reply engages it, or
   when it is on a DOCUMENTATION path.
   **`_is_doc_path` is ANY prose extension at ANY depth — not an allowlist of
   directories.** OWNER DECISION 2026-09-04 (`815e0dbd2`, #1689) widened it:
   `.md`/`.markdown`/`.rst`/`.adoc` anywhere, plus a known doc STEM
   (CHANGELOG/README/LICENSE/…) with `.txt` or no extension. So `AGENTS.md`,
   every `SKILL.md`, `CLAUDE.md` and all of `.claude/**/*.md` ARE doc paths — and
   with the default `_DEFAULT_DOC_FINDINGS_MODE = "skip"` a finding on one NEVER
   scores, at any severity. Findings are still SURFACED in every mode; the lever
   decides only whether they COUNT.
   ⚠ This paragraph previously stated the exact opposite — that those files
   "return False from `_is_doc_path`, so a P1 anchored on one contributes its
   full 1.0" — describing the pre-#1689 world. It was itself a correction of an
   earlier error (see the callout below) and went stale the same way, which is
   the point: **verify a gate claim against the symbol before relying on it, and
   distrust this file most where it sounds most certain.**
   Pure WARNINGs/NOTEs (non-P1/P2) → merge allowed.
   **This paragraph used to say "any P1" blocks, which was FALSE, and the
   divergence cost a whole session.** A P1 anchored on `CHANGELOG.md` merged
   (#1606, 2026-09-03) with no override; a session read this text, saw the merge,
   concluded the gate had a hole, and built a reversal of the owner's directive
   before checking whether one existed. Docs describe intent, code describes
   reality — and when they disagree, suspect the doc. Verify against
   `git_push_guard.py` before concluding the gate misbehaved.

   **A doc-path P1 is still usually a CODE finding.** A changelog is where a PR
   states its own blast radius, so a P1 there typically means *"your stated blast
   radius is wrong"* — the anchor is prose, the defect is not. #1606's was
   anchored on the sentence "the quiet path is a hand-edited overlay", which was
   false: the settings writer persisted `default: glm-5.2` alone, so settings-UI
   users hit the quiet path too. Correctly non-blocking; still needed fixing.

## Lane classifier

   The light lane is `.md`/`.rst`/`.markdown`/`.adoc`, plus `.txt` and the
   extensionless form ONLY on a known documentation stem (`CHANGELOG.txt` and a
   bare `LICENSE` yes, `requirements.txt` no — the same split `_is_doc_path`
   makes), plus tests and fixtures (`review_scope._is_lane_light`). Those
   spellings are matched DIRECTLY rather than behind `_category`, because
   `_category` calls `.adoc` and an extensionless `README` **code** — so an
   earlier draft that listed them behind it advertised prose formats nothing
   could reach.

   **A PROMPT SURFACE is prose here.** `_category` calls `SKILL.md`,
   `.claude/commands/*.md` and `src/genesis/skills/**/*.md` *code*; the lane
   reads them as what they are. MEASURED: this is the single biggest effect of
   the lane's own vocabulary — 14 of 40 recent PRs classify `light` where the
   inherited tagger said `standard` — and it is mostly INERT, because 11 of those
   14 touch nothing whose findings score at all (every path is an `_is_doc_path`
   and `doc_findings` defaults to `skip`). Where it bites is a prose-plus-TESTS
   PR, whose test findings then clear at 3.0.

   MEASURED over the 40 most recently merged PRs:
   critical 35.0%, standard 20.0%, light 45.0% (a SLIDING window — see the
   classifier docstring; re-running will not reproduce it, and a difference is
   the merge queue moving, not a regression). The lane comes from
   `review_scope.classify_lane`, which FAILS CLOSED to `critical` on an
   unreadable file list — the lane relaxes a threshold, so the safe default is
   the one that relaxes nothing.

   ⚠ **A stable distribution is NOT a coverage proof, and neither is a passing
   example.** Building this lane produced the same miss twice, each time caught by
   a method the previous one could not reach:

   * A draft held critical at an unchanged 30.0% while having silently stopped
     classifying `*route*`/`*controller*`/`*endpoint*` paths as critical — same
     percentage, different membership, because none of the 40 sampled PRs touched
     such a file. A **constructed test case** found it; the measurement could not.
   * Four route-defining modules — `src/genesis/hosting/**` and
     `dashboard/_blueprint.py`, one serving `/genesis/login` — were still outside
     the lane after that fix. Every constructed case passed and the distribution
     reproduced to the decimal. Only an **enumeration over every tracked module**
     found them, which is why that class is now locked by a population check
     rather than by more examples.

   When you change what feeds a classifier: diff the per-item ASSIGNMENTS rather
   than the totals, and lock a category by enumerating its population rather than
   by naming the members you happened to think of.

## Override log

   The override is logged — one metadata row per sigil, written as one file per
   merge under `~/.genesis/merge_overrides/` (sigil, PR, bound head, which gate it
   waived, and the verdict THAT GUARD returned: `allowed` / `asked` / `blocked` /
   `error`), so "was this escape reached for?" is a query rather than a survey:
   `cat ~/.genesis/merge_overrides/*.jsonl`. One file per merge rather than one
   shared log, so a multi-sigil merge is a single atomic record and the writer
   never maintains a file on the merge path. The store is bounded to 5 MB by the
   daily `disk_hygiene.sh` timer (`scripts/prune_hook_audit_logs.py`, oldest whole
   records dropped) and backed up by `backup.sh` §6d.
   `blocked` matters: a sigil can be appended to a command that some OTHER gate
   inside the same guard then stops, which is a real signal but not an override
   that took effect, and `asked` means the decision went to a human who may still
   have denied it. Read `allowed` as "the sigil was accepted HERE", never as "the
   merge happened": other PreToolUse matchers and the harness's own permission
   prompt sit downstream of this guard and can still stop the command.
   Never any command text — the row is metadata only, because a trailing comment
   rides a Bash command that can carry a credential.
   **Scope, stated so the log is not read as more complete than it is:** the six
   PR-merge sigils are covered (`# review-override`, `# ci-override`,
   `# stale-review-override`, `# scheduled-review-override`,
   `# substitute-review`, `# rework-override`), from the point the
   PR is resolved onward. A merge rejected BEFORE that point writes nothing —
   no `--admin`, an unresolvable repo/PR, a compound carrying two publish/merge
   operations, or a merge compounded with a local `git merge` into main are the
   ones measured, and the list is illustrative rather than exhaustive: anything
   that blocks before the PR resolves leaves no row. No OTHER sigil in
   `shell_parse._KNOWN_SIGILS` is recorded either — `# depth-ack`, `# audit-ack`,
   `# discard-override` and `# full-suite-ok` belong to gates outside this guard
   and announce themselves on stderr only. The command-scoped acks
   (`# merge-to-main-override`, `# escalation-ack`) are
   NOT logged, for two different reasons. `# merge-to-main-override` waives a
   gate that short-circuits BEFORE the branch is resolved, so a row could only
   say the ack was typed, never that it waived anything. The local
   `# escalation-ack` is outside this guard. `# final-round-accept` remains
   parser-compatible for stale worktrees but authorizes no current gate. Native round
   approvals are decisions rather than sigils and are deliberately not written as
   reusable receipts.

## Scheduled-review inventory

  Why an inventory and not a diagnosis: the previous shape picked one cause per kind and
  hid the rest, and every one of nine review findings across six rounds was a hidden fact
  — a refused `[P1]` on an older commit hidden behind a typo at the current one, the only
  evidence a review had ever run hidden by a drive-by comment. Precedence is the right
  shape for a VERDICT; for a REPORT, hiding a true fact is never correct.

## CI traps

9. **`gh run rerun` CANNOT clear a failure caused by a broken base.** It replays
   the ORIGINAL merge commit rather than recomputing one against current main,
   so re-running a PR whose base was broken faithfully reproduces the breakage.
   MEASURED: a re-run started 15 minutes AFTER the fix merged still failed, and
   `git merge-base --is-ancestor <fix-sha> refs/pull/<N>/merge` returned false —
   the pull request's merge ref did not contain the fix. Only a push or
   `gh pr update-branch <N>` reliably recomputes it, and both move the head.
   Closing and reopening the PR is NOT a substitute. MEASURED 2026-10-01: a
   reopen fired a new CI run that checked out a merge ref GitHub had built about
   three and a half hours earlier, on a main several merges behind. Whatever you
   try, read the job log's `HEAD is now at <merge sha>
   Merge <head> into <base>` line and confirm the base is the main you expect.
   "CI re-fired" is not evidence that it tested current main. Two consequences: do not "just
   re-run" someone's stale red, and do not tell them it will clear — it will
   not, and their next push clears it for free anyway.

10. **`gh run view --log-failed` truncates and shows only PASSED lines.** On a
    large suite it cuts around 44%, so a FAILING job reads as a clean log with
    no failure in it — the most misleading possible output, because it looks
    like an answer. It reported nothing useful on three separate attempts in one
    session. To find what actually failed, reproduce locally against a clean
    `origin/main` checkout (which is what identified it, twice), or fetch the
    raw job log rather than the CLI's rendering.

11. **Two individually-GREEN PRs can break main, and nothing warns you.** PR A
    adds a repo-wide invariant test (one that ENUMERATES modules or consumers and
    asserts a property); PR B adds a violation of it. Each is green against a
    main that lacks the other, neither diff touches the other's files, so there
    is no textual conflict, no reviewer signal, and both merge clean — then main
    goes red, and because PR CI builds the merge commit it fails EVERY open PR on
    a test unrelated to their changes. MEASURED 2026-09-08: a chokepoint test
    requiring every consumer of the shared shell parser to use the checked entry
    point, and a guard importing the unchecked one, merged an hour apart.
    This is NOT the structural collision described under the architecture-session
    rule (closing-session, "Some PRs are not a review problem") — that one is main superseding a mechanism the PR uses, and it is
    visible from the PR's own files. Here neither PR can see the other.
    The detectable signature is a new ENUMERATING invariant test landing while
    other PRs are in flight: when you merge one, re-run it against the other open
    PRs' merge commits before merging them, not just against its own branch.
    Sequencing follows from the same fact — batch merges rather than interleaving
    them with active pushes, because each merge invalidates every other PR's
    mergeability and manufactures the false "CI never ran" signal (closing-session, Pre-Merge Gate, "A CONFLICTING PR silently suppresses the whole CI suite").
