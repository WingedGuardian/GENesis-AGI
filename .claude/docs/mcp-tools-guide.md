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
`tabled`). This section is the operational detail behind it.

### Filing the issue

```bash
gh issue create --repo <owner>/<public-repo> \
  --label area:<domain> --label <difficulty-or-environment>
```

**Always pass `--repo`.** `gh` resolves the target from the git remote, and the
documented install is a fork clone (`.claude/docs/your-genesis.md`) — so on most
installs a bare `gh issue create` files to the user's PRIVATE fork, where nobody can
pick it up, silently and with no error. The target is `github.user` + `github.public_repo` in
`~/.genesis/config/genesis.yaml` (resolvers: `genesis.env.github_user()` /
`github_public_repo()` — the latter returns the bare name, without the owner).

**Both label classes are mandatory**, matching the policy the propose path enforces
fail-closed (`src/genesis/mcp/health/contributor_issue.py`): an `area:*` domain label
AND a difficulty/environment label (`good first issue`, `first-timers-only`,
`help wanted`, `needs-genesis-instance`). The hand path has no validator, so this is on you — an
unlabelled issue silently breaks an invariant the codebase enforces elsewhere.

**Privacy-scan the body first.** Technical detail only: no IPs, hostnames, absolute
home paths, identifiers, or raw install metrics (row counts and spend leak usage
scale — state proportions or shapes instead). Nothing gates this path — `gh issue
create` appears nowhere in `scripts/hooks/git_push_guard.py`, which gates push /
`gh pr merge` / `gh pr create` — so a public post here is irreversible and unguarded,
and must be a conscious, human-present act.

### Why not `contributor_issue_propose`

That MCP tool is the *gated* door: fail-closed prose sanitizer, owner approval hold,
label validation, dedup and rate backpressure, and a `source_follow_up_id` close-loop
link. It is the better path when it applies — but it ships `propose_only` by default,
where an approved hold still dry-runs and never posts, and a `dry_run` mark is
terminal (flipping the lever to `live` later does NOT retro-post). So it cannot serve
"file this now". Use it for curated contributor-facing work; use the hand path above
for engineering issues you need on the tracker today.

### Dispatched / background sessions

A dispatched session must NOT file issues — no gate covers the path and no human is
present. (`steward` gets `gh`-restricted Bash, so for that profile this is a policy
line, not a locked door.) It records locally instead, but check the profile first
(`.claude/docs/background-sessions.md`): **naming a route a session cannot take is
worse than naming none.**

Check your OWN denylist rather than trusting a list here — profiles change. Three
tiers exist today:

- HAS `follow_up_create` (`interact`, `research`, `campaign`, `steward`; and
  `sentinel` when NOT degraded — `_DEGRADED_DISALLOWED_TOOLS` denies no MCP tools)
  → file there, and say in `reason` that it is repo work awaiting a foreground
  session. Note the row is FORCED onto the cold `tabled` lane by sacred-board
  authorization whatever `work_state` you pass, and tabled rows are excluded from
  every default listing — retrieve with `follow_up_list(include_tabled=True)`.
- HAS `observation_write` but NOT `follow_up_create` (reflection sessions, whose
  denylist derives as `live registry − read-allowlist − observation_write`) → use
  `observation_write` or the parsed `observations` output field.
- Has NEITHER (`observe`, `community-responder`, `mail` — all carry
  `_NO_MEMORY_WRITES` + no follow-ups; and sentinel-DEGRADED, which mounts no MCP at
  all) → there is no local record to write. Return it in the session's output text
  and let the dispatching foreground session file it.

There is no automated promoter from a `tabled` row to an issue today. A foreground
session has to carry it across.

### The time-gated exception

A GitHub issue has no revisit mechanism — the trigger machinery (`blocked_on_trigger`,
`revisit_condition`, `scheduled_at`, the 5-minute dispatcher) is ledger-only. So repo
work that is ALSO gated on a time or event keeps **both**: the issue, plus a local
`blocked_on_trigger` row carrying the trigger. That row REQUIRES a `revisit_condition`
(the tool hard-errors without one) and cannot use `strategy="surplus_task"`.

This is the one case where two records for one item is correct. Otherwise: one record.

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
