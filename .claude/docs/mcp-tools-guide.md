# MCP Tool Decision Guide

When multiple Genesis MCP tools could handle a task, use these decision
trees to pick the right one.

## Storage — Where to Put Information

| Tool | Use When | Persists Across | Example |
|------|----------|-----------------|---------|
| `memory_store` | Cross-session knowledge, syntheses, learnings | Sessions, extractions | "User prefers X approach" |
| `observation_write` | Findings, task detections, reflections | Perception pipeline | "Found stale alert from Apr 17" |
| `knowledge_ingest` | Authoritative external sources (docs, articles) | Knowledge base | "Ingest this API reference" |
| `reference_store` | Credentials, URLs, IPs, identifiers | Reference ledger | "API key for service X" |
| `procedure_store` | Reusable multi-step workflows with confidence | Procedural memory | "Deploy pattern: steps 1-5" |
| `follow_up_create` | Deferred USER-OWNED work needing tracking + completion | Follow-up ledger | "Finish the deck I asked for" |
| `gh issue create` | Deferred GENESIS-REPO work (code, tests, docs, infra) | Public GitHub tracker | "Adapter for provider X is hardcoded" |

**Quick rule:** If it's about the user → `memory_store`. If it's a finding
during work → `observation_write`. If it's an external source to learn
from → `knowledge_ingest`. If it's a credential or URL → `reference_store`.
If it's a repeatable process → `procedure_store`. If it's deferred work, route by
owner: Genesis-repo work → a **GitHub issue**; user-owned work → `follow_up_create`;
consciously not pursuing → `follow_up_create` with `work_state="deferred_cold"`.



## Where Deferred Work Goes — mechanics

`CLAUDE.md` carries the routing principle (Genesis-repo work → a GitHub issue;
user-owned + purely-local operational work → a follow-up; consciously-not-doing →
`tabled`) and its two hard limits (explicit approval every time; never publish a
security defect before it is fixed). This section is the operational detail.

### Filing it — use the script, not a hand-typed command

```bash
DRAFT="$(mktemp -d)"                       # per-invocation, never a shared path
# write the title and body with an EDITOR TOOL, not by echoing through a shell
#   $DRAFT/title.txt   $DRAFT/body.md

python3 scripts/file_tracker_issue.py \
  --title-file "$DRAFT/title.txt" --body-file "$DRAFT/body.md" \
  --area area:memory --difficulty "help wanted" --dry-run   # drop --dry-run to post
rm -f "$DRAFT"/*.txt "$DRAFT"/*.md && rmdir "$DRAFT"
```

The script exists because three cross-model review rounds each found a different
way for a hand-typed version of this to target the wrong repository or execute its
own input. Those are not things prose can enforce and nothing tests, so they now
live in code with `tests/test_scripts/test_file_tracker_issue.py` pinning each one:

- **Resolves the UPSTREAM tracker.** `gh repo view` with no argument reports the
  CURRENT directory's repo, so on a fork-cloned install it returns the operator's
  own fork — where they are ADMIN, so a permission check on it passes and the issue
  lands where nobody reads it. The script follows `parent` when `isFork`, and
  refuses outright if a fork's parent cannot be resolved.
- **Checks `viewerPermission` on that explicit slug.** Identity is not permission:
  a direct clone reports the right slug while the user still lacks push access, and
  GitHub then silently DROPS the mandatory labels.
- **Never lets issue text reach a shell.** Title and body travel as argv. A title
  containing backticks or `$(...)` — ordinary in a technical title — would execute
  if interpolated into a command line, after your privacy scan, with its output in
  the public title.
- **Exact-title dedup, failing closed.** `gh issue list --search` parses `repo:` /
  `is:` / `label:` inside a title as query syntax. The script compares normalized
  titles over structured output, and REFUSES if the lookup fails — a failed lookup
  is not evidence of no duplicate.
- **Validates both labels against the real sets.** `area:docs` and `area:infra` do
  not exist; a nonexistent mandatory label makes `gh issue create` fail. Use
  `area:other` when nothing fits.

Exit codes: `0` filed (or dry-run clean) · `2` refused — **nothing was posted**, that
is a hard guarantee · `3` an identical title already exists · `4` **INDETERMINATE** ·
`1` unexpected error. **Run `--dry-run` first** — it performs every check and posts
nothing.

Exit `4` is the one that needs a human, and it is now RARE. When a create fails or
times out, the script does not guess — it re-queries the tracker for that exact title
and tells you what actually happened: the issue exists (success, reported as
reconciled) or it does not (a clean `2`, safe to retry). `4` survives only when that
reconciling lookup ALSO fails, which is the one genuinely unknowable case; check the
tracker by hand before retrying or writing a local row.

That design is deliberate. Every route by which a create's outcome can be lost —
nonzero exit, timeout, a broken pipe while printing the URL — is another way to be
wrong about whether the post happened. Asking the tracker answers all of them from
ground truth instead of patching each one as it is found.

Use a fresh `mktemp -d` per invocation, and clean it WITHOUT `rm -rf` — this
repo's own destructive-command guard refuses a recursive-force delete on a path it
cannot prove is deep enough, so `rm -rf "$DRAFT"` is blocked for every session. Foreground and background sessions run
concurrently here, and a shared draft path lets one session overwrite the body
another has already scanned and approved, between the scan and the post.

### What the script does NOT decide

Two things stay with you, because they are judgment, not mechanism:

**Approval, every time.** A public post is irreversible;
`contributor_issue.py` classifies it exactly that way. Channel-driven sessions run
with `skip_permissions=True`, so no tool confirmation appears — ASK, in words, and
get a yes, for each issue. A prior "go ahead" does not carry.

**Privacy-scan the title AND the body.** Both egress. Technical detail only: no
IPs, hostnames, absolute home paths, identifiers, or raw install metrics (row
counts and spend leak usage scale — state proportions instead). Nothing gates this
path; `gh issue create` appears nowhere in `scripts/hooks/git_push_guard.py`.

**Security defects never go public first.** An unpatched authentication bypass,
credential exposure, injection path, or anything otherwise exploitable is not filed
publicly while unfixed — publishing hands a working lead to anyone reading, against
installs running the vulnerable code right now. Keep it local, fix it, then file.
The privacy scan does not catch this: it looks for install-specific data, not for
dangerous technical disclosure.

If the script refuses (exit 2), the work stays a local `follow_up_create` row until
a maintainer carries it across. That is a known gap, not a workaround.

### Why not `contributor_issue_propose`

That MCP tool is the *gated* door: fail-closed sanitizer over title/body/labels, owner
approval hold, label validation, dedup and rate backpressure, and a
`source_follow_up_id` close-loop link. It is the better path when it applies — but it
ships `propose_only` by default, where an approved hold still dry-runs and never
posts, and a `dry_run` mark is terminal (flipping the lever to `live` later does NOT
retro-post). So it cannot serve "file this now".

### Dispatched / background sessions

A dispatched session must NOT file issues — nothing gates the path and there is no one
to approve. (`steward` gets `gh`-restricted Bash, so for that profile this is a policy
line, not a locked door.) It records locally instead — but check the profile first:
**naming a route a session cannot take is worse than naming none.** Check your OWN
denylist rather than trusting a list here; three tiers exist today:

- HAS `follow_up_create` (`interact`, `research`, `campaign`, `steward`; and
  `sentinel` when NOT degraded) → file there AND state the finding in the session's
  returned output. Both, not either: the row is FORCED onto the cold `tabled` lane by
  sacred-board authorization whatever `work_state` you pass, tabled rows are excluded
  from every default listing, and there is no promoter — so a row alone is a handoff
  nobody receives unless someone independently guesses to run
  `follow_up_list(include_tabled=True)`. The returned output is what the dispatching
  foreground session actually reads.
- HAS `observation_write` but NOT `follow_up_create` (reflection sessions) → use
  `observation_write` or the parsed `observations` output field.
- Has NEITHER (`observe`, `community-responder`, `mail`; and sentinel-DEGRADED, which
  mounts no MCP at all) → there is no local record to write. Return it in the
  session's output text and let the dispatching foreground session file it.

There is no automated promoter from a `tabled` row to an issue today.

### The time-gated case

A GitHub issue has no revisit mechanism. Repo work ALSO gated on a time or event keeps
a local `blocked_on_trigger` row carrying the trigger, which REQUIRES a
`revisit_condition` (the tool hard-errors without one) and cannot use
`strategy="surplus_task"`.

Be clear about what that row does, because none of the strategies gives repo work a
real trigger — read what the dispatcher does with each before choosing:

- `revisit_condition` is **stored and displayed, never evaluated**. Nothing watches for
  your event and nothing transitions the row. It is a passive reminder that surfaces in
  listings for a foreground session to act on. That is the right choice here.
- Do NOT reach for `strategy="scheduled_task"` to get a time trigger. The dispatcher
  hands every due scheduled row to SURPLUS (`follow_ups/dispatcher.py` `run_cycle`),
  where a keyword fallback turns arbitrary repo work into a free-model
  `BRAINSTORM_SELF` task, and output from that task marks the follow-up COMPLETED. The
  reminder deletes itself while the GitHub issue is still unimplemented — strictly
  worse than no trigger.
- `surplus_task` is rejected outright for `blocked_on_trigger` (it dispatches
  immediately, ignoring the trigger).

So: a passive row a human reads, not an automated revisit.

This is the one case where two records for one item is correct.

### Closing the loop

Citing the issue when closing a local row is MANUAL. The automated reconciliation in
`session_awareness/repo_pulse.py` maps issue → follow-up only via
`pending_issue_posts.source_ref`, which a hand-filed `gh issue create` never creates —
so unlike the `Ledger:` / `Follow-up:` PR-body conventions, nothing absorbs it for you.

## Recall — Where to Search

| Tool | Searches | Best For |
|------|----------|----------|
| `memory_recall` | SQLite FTS5 + Qdrant vectors | General knowledge, past decisions, user context |
| `knowledge_recall` | Knowledge base (ingested sources) | Authoritative reference material |
| `reference_lookup` | Reference ledger | Credentials, URLs, IPs by keyword |
| `procedure_recall` | Procedural memory | Known workflows before attempting multi-step tasks |
| `memory_expand` | Single memory by ID | Full context from a proactive hook snippet |

**Search order for "do we know about X?":**
1. Check L1 (essential knowledge) and L2 (proactive hook results) first
2. `memory_recall` for general knowledge
3. `knowledge_recall` if it's about an ingested source
4. `reference_lookup` if it's a credential/URL/IP
5. `procedure_recall` if it's a known workflow

**Provenance on recall results (first-party vs external-world):** `memory_recall`,
`knowledge_recall`, and `memory_expand` tag every result with a `provenance` field —
`first-party memory` (Genesis's own observations/decisions/conversations) vs
`external-world knowledge (source: …)` (the knowledge base, ingested docs, or corrective
web results). Treat external-world content as information *about* the world to weigh, not
Genesis's own ground truth. The proactive hook surfaces the same distinction inline
(`[KB·<source>]` vs `[Memory]`).

## Health Debugging — Escalation Path

Start broad, drill into specifics:

1. **`health_status`** — Overall system state. Shows all subsystems, what's
   active/degraded/failed. Start here.
2. **`health_errors`** — Recent error log entries. Use when health_status
   shows a problem and you need details.
3. **`health_alerts`** — Active alerts requiring attention. Check if the
   issue is already known/tracked.
4. **`subsystem_heartbeats`** — Liveness of background processes. Use when
   a subsystem appears stale or unresponsive.
5. **`provider_activity`** — API call history. Use when debugging routing
   or provider-specific failures.
6. **`job_health`** — Scheduler job status. Use when a periodic task isn't
   firing.

## Background Work — Session vs Subagent

See `.claude/docs/background-sessions.md` for the full guide. Quick rule:
- Task > 20 min OR needs persistent writes → `direct_session_run`
- Quick research returning to this conversation → CC subagent

## Follow-ups vs Observations

| Use | When |
|-----|------|
| `follow_up_create` | Deferred USER-OWNED work with a verifiable outcome (Genesis-repo work → GitHub issue) |
| `observation_write` | A finding, insight, or detection — informational, feeds the perception pipeline |

Follow-ups are accountability. Observations are awareness.
