---
name: integrate-module
description: Turn any external program into a Genesis module via structured discovery, connection mapping, config generation, and verification.
consumer: cc_foreground
phase: 7
skill_type: workflow
---

# Integrate Module

Turn any external program into a Genesis module. This is the standardized
process for plugging a program into Genesis — whether it was built for Genesis
or not.

Genesis is the nervous system. The program is the body. This skill builds the
connection between them.

## When to Use

- User says "integrate X into Genesis" or "make X a module"
- User wants to plug an external program, service, or tool into Genesis
- User wants Genesis to drive, monitor, or interact with an external system
- Ego session discovers a program worth integrating and proposes it

## Input

One of:
- **Running service URL** (e.g., `http://${LOCAL_HOST:-localhost:8080}`)
- **Repository URL** (e.g., `github.com/user/repo`)
- **Local path** (e.g., `~/my-tool/`)
- **Description** (e.g., "a job automation tool running on my server")

## Phase 1: Discovery

**Goal:** Understand what the program does, how it communicates, and what
Genesis can connect to.

### If running service:
1. Probe for health endpoints: `/health`, `/api/health`, `/status`, `/`
2. Probe for API documentation: `/docs`, `/openapi.json`, `/swagger.json`
3. If OpenAPI spec found: parse all endpoints, methods, parameters
4. If no spec: probe common REST patterns, check response shapes
5. Record: base URL, available endpoints, response formats, auth requirements

### If repository:
1. Clone or read the source
2. Identify tech stack (language, framework, database, scheduler)
3. Find entry points: route definitions, CLI commands, main functions
4. Map API endpoints from source (FastAPI routes, Express handlers, etc.)
5. Identify configuration: env vars, config files, secrets needed
6. Check deployment method: Docker, systemd, kubernetes, manual
7. Record: full capability map from source

### If description only:
1. Ask the user for more details: where does it run? how do you access it?
2. Attempt to locate the service or source based on what they share
3. Fall back to manual endpoint mapping with user guidance

### Output of Phase 1:
A structured assessment:
```
Program: [name]
Location: [URL / path / host]
Tech Stack: [language, framework, DB]
Communication: [HTTP API / CLI / stdin / socket]
Endpoints Found: [count]
Auth Required: [yes/no, method]
Health Check: [endpoint, expected response]
```

## Phase 2: Connection Mapping

**Goal:** Map every program capability to a Genesis module operation.

For each discovered endpoint or command:
1. What does it do? (read data, trigger action, modify state)
2. What parameters does it take?
3. What does it return?
4. Is it safe to call autonomously? (read-only vs. state-changing)
5. Should it be in the operations manifest?

Group operations by category:
- **Health/Status** — monitoring operations (always include)
- **Data Retrieval** — read-only queries (safe for autonomous use)
- **Actions** — state-changing operations (may need user approval)
- **Admin** — deployment, configuration, lifecycle (restricted)

### Output of Phase 2:
Draft operations manifest (YAML format):
```yaml
operations:
  health:
    method: GET
    path: /api/health
    description: "Check service health"
  # ... all discovered operations
```

## Phase 3: Effort Assessment

**Goal:** Give the user an honest picture of what's needed.

Assess each dimension:
- **Connectivity** — Can Genesis reach the program? (network, auth, firewall)
- **API Coverage** — What % of the program's capabilities are API-accessible?
- **Health Monitoring** — Is there a health endpoint? Can we detect failures?
- **Lifecycle** — Can Genesis start/stop/restart the program? (SSH, systemd, k8s)
- **Data Flow** — Can Genesis read the program's data? Push data to it?
- **Autonomy Potential** — What can Genesis do without user approval?

Rate complexity:
- **Simple** (1 session): Program has HTTP API, health endpoint, clear operations.
  Genesis wraps it as-is. Career Agent was this level.
- **Moderate** (2-3 sessions): Program needs some adapter work. Maybe API gaps,
  authentication setup, or custom endpoint mapping.
- **Complex** (multi-session): Program needs code changes. Missing API for key
  capabilities, no health endpoint, custom IPC needed, authentication integration.

Be explicit: "This integration will take [estimate]. Here's why: [specific gaps]."

If complex, recommend incremental approach:
1. Start with health monitoring + basic queries (one session)
2. Add action operations (second session)
3. Add lifecycle management (third session)
4. Add deep bidirectional capabilities (ongoing)

## Phase 4: Config Generation

**Goal:** Create the YAML module config and any needed adapter code.

1. Generate `config/modules/[program-name].yaml`:
   - name, type: external, description
   - IPC method and URL
   - Health check endpoint
   - Lifecycle commands (if SSH available)
   - Full operations manifest from Phase 2
   - Configurable fields (if applicable)

2. If the program needs custom adapter code beyond what ExternalProgramAdapter
   provides, create it in `src/genesis/modules/[program-name]/`.

3. Test the connection: verify health check passes.

## Phase 5: Dashboard Setup

**Goal:** Make the module usable from the Genesis dashboard.

Verify:
- Module appears in Modules & Providers panel with EXTERNAL badge
- Description is populated from YAML
- Health status shows Connected/Disconnected
- IPC details visible (method, URL)
- Lifecycle commands available (if configured)
- Clicking the module opens a useful detail modal

If the program has unique data worth displaying, consider adding custom
dashboard elements (follow-up, not blocking).

## Phase 6: Verification

**Goal:** Prove the integration works end-to-end.

1. **Health check**: `module_call("[name]", "health")` returns healthy
2. **Data query**: Test a read-only operation via `module_call`
3. **Action** (if applicable): Test a state-changing operation
4. **Dashboard**: Module shows correct status, description, operations
5. **Persistence**: Restart bridge, verify module re-loads from YAML
6. **Error handling**: What happens when the program is unreachable?

## Phase 7: Documentation

1. Update `MEMORY.md` with the new module entry
2. If the integration required non-obvious decisions, record them
3. Commit all changes via worktree, merge to main

## Notes

- Every integration is bespoke. This skill is the map, not the destination.
- The depth of integration is the user's call. Genesis maps the terrain and
  presents options. The user decides how far to go.
- Don't gatekeep. If the user says "do it all," do it all. If they want
  just health monitoring, that's fine too.
- Be honest about complexity. "This will take multiple sessions" is useful
  information, not a reason to avoid starting.
- The goal is full co-pilot capability: anything the user could do with the
  program, Genesis should be able to do through the module interface.
