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
| `follow_up_create` | Deferred work that needs tracking + completion | Follow-up ledger | "Benchmark new model next week" |

**Quick rule:** If it's about the user → `memory_store`. If it's a finding
during work → `observation_write`. If it's an external source to learn
from → `knowledge_ingest`. If it's a credential or URL → `reference_store`.
If it's a repeatable process → `procedure_store`. If it's deferred work →
`follow_up_create`.

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
| `follow_up_create` | Deferred work with a verifiable outcome — needs tracking through completion |
| `observation_write` | A finding, insight, or detection — informational, feeds the perception pipeline |

Follow-ups are accountability. Observations are awareness.
