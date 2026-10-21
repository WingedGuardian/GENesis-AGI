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
