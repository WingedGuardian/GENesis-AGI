---
name: subsystem-map
description: >
  This skill should be used before answering "does Genesis have X", "does
  Genesis lack X", auditing Genesis capabilities, comparing Genesis to an
  external system, or reviewing/summarizing the architecture. It routes to
  the canonical judgment-layer subsystem map so audits start from the map,
  not from a cold grep. Also fires after changing a subsystem's
  capabilities, to keep the map current.
keywords: [capability, capabilities, subsystem, map, audit, architecture, lacks, missing, compare, competitive]
---

## The Rule

**Read `docs/architecture/CURRENT.md` FIRST** — before any grep, agent
dispatch, or claim about what Genesis has or lacks. It is the canonical
judgment-layer map: what each subsystem is FOR, its easy-to-forget
mechanisms, maturity (live / shadow / dark), and do-not-touch edges.

A claim of absence ("Genesis lacks X", "Genesis should add X") made
without consulting the map first is a protocol violation — the 2026-06-30
audit produced 6/7 wrong infrastructure claims exactly this way.

## Workflow

1. **Locate the entry.** Find the subsystem entry (or entries) covering the
   capability in question. Entries claim modules in fenced
   `yaml subsystem-map` blocks — the union covers every top-level package
   in `src/genesis`, so "no entry mentions it" means you're using the wrong
   concept name, not that it doesn't exist.
2. **Trust but verify.** The map is judgment-layer, not ground truth. Check
   each entry's `verified: <sha> <date>` stamp; if its modules have moved
   since, re-verify the specific claim against the live tree before relying
   on it.
3. **Then enumerate.** For absence/existence conclusions, follow the audit
   protocol (procedure `codebase_audit` / dev-skill "Auditing Existing
   Capabilities"): enumerate the subsystem's module inventory, trace the
   call graph both directions, grep by concept with synonyms, and verify
   against RUNTIME state (env gates, logs) — the map tells you where to
   look, not what to conclude.

## Write-Back Duty

After you **change** a subsystem's capabilities (new mechanism, new gate,
wiring flip, removal) or **re-verify** an entry during an audit:

- Update the owning entry's prose and its `modules:` block if the package
  set changed.
- Bump the entry's `verified:` stamp to the current short sha + date.

CI enforces the module partition (`scripts/check_subsystem_map.py`): a new
top-level package that no entry claims fails the build; stale stamps warn.

## Naming Trap

`capability_map` (DB table) and `ego/capability_aggregator.py` are the
ego's per-domain self-confidence model — completely unrelated to this map.
Never conflate them; everything here is "subsystem map".
