# Genesis Module Paradigm

**Status:** Active | **Last updated:** 2026-04-19
**Decision source:** User-directed architectural decision, 2026-04-19

---

## Core Principle

A module is a tool that Genesis wields. Genesis is sovereign; modules
serve Genesis, not the other way around. No module — internal or
external — commandeers Genesis's cognitive infrastructure.

This document defines the two-tier module model, trust boundaries,
and the enforcement mechanisms that keep modules properly scoped.

---

## Two-Tier Module Model

### Internal Modules

**The module IS the program.** It runs inside Genesis's process space,
uses Genesis's database and event bus, and registers operations through
the module protocol.

Examples: prediction markets, crypto ops, future domain-specific tools.

**What internal modules CAN do:**
- Register operations via `ModuleProtocol`
- Use Genesis's DB for module-specific tables
- Emit and subscribe to events on the event bus
- Use registered providers (search, fetch, embeddings)

**What internal modules CANNOT do:**
- Use cognitive call sites (memory extraction, reflection synthesis,
  knowledge distillation, triage reasoning) — these serve Genesis's
  own thinking, not module work
- Modify routing weights or circuit breaker state
- Access other modules' registered operations directly
- Write to memory/observation stores without going through the
  module protocol's output channel

The boundary for internal modules is the module protocol interface.
Domain logic stays inside the module; cognitive infrastructure stays
inside Genesis.

### External Modules

**The module is a BRIDGE to a separate program.** The external program
runs independently — different machine, different stack, different
lifecycle. The module mediates all communication between the program
and Genesis.

Examples: career (career-ops + jerbs), future third-party integrations.

**Architecture:**
```
External Program  →  Module (bridge)  →  Genesis (brain)
                  ←                    ←
```

**What external programs CAN do:**
- Send signals to the module's inbound endpoint (HTTP IPC or stdio)
- Receive pushes from Genesis through the module's outbound channel

**What external programs CANNOT do:**
- Access Genesis MCP servers directly
- Query Genesis memory, observations, or knowledge
- Read Genesis's database
- Invoke Genesis cognitive infrastructure
- Anything not explicitly exposed through the module's IPC contract

The boundary for external modules is the `ExternalProgramAdapter` +
`HttpIPCAdapter` (or `StdioIPCAdapter`) interface. The module defines
the signal contract; Genesis decides what to do with inbound signals.

---

## Trust Model: Asymmetric by Design

The trust relationship between Genesis and modules is deliberately
asymmetric:

| Direction | Trust Level | Mechanism |
|-----------|-------------|-----------|
| **Genesis → internal module** | Full control | Genesis registers, configures, enables/disables, and can modify module code |
| **Internal module → Genesis** | Protocol-only | Module uses registered interfaces; cannot reach cognitive internals |
| **Genesis → external program** | Full reach | Genesis can read files, edit configs, SSH in, push changes through module. Override authority. |
| **External program → Genesis** | Signal-only | Program posts signals to module inbound endpoint. Genesis considers and decides. |

Genesis always has override authority. It can reach into any module or
external program when it decides to. The reverse is never true — modules
and programs interact with Genesis only through their designated
interfaces.

---

## Enforcement: "Cognitive Architecture Is Not a Service"

The design principle "cognitive architecture is not a service" (from
CLAUDE.md) is enforced at the module boundary:

1. **Call site scoping.** LLM call sites in `genesis.routing` are tagged
   with their purpose (e.g., `reflection_synthesis`, `fact_extraction`,
   `knowledge_distillation`). These are Genesis's cognitive processes.
   Modules that need LLM calls use external providers or their own
   call sites — never Genesis's internal ones.

2. **Memory isolation.** Modules don't write directly to Genesis memory
   stores. They emit signals/events that Genesis's own cognitive
   processes (triage, learning, reflection) decide whether to store.

3. **IPC as contract.** External modules define explicit IPC contracts
   (inbound signals, outbound pushes). The contract is the boundary.
   Anything not in the contract doesn't cross.

4. **Module lifecycle.** Genesis controls module registration, health
   checks, enable/disable state, and configuration. Modules cannot
   self-register or modify their own config without Genesis mediation.

---

## Implementation Reference

| Component | Location | Purpose |
|-----------|----------|---------|
| `ModuleProtocol` | `src/genesis/modules/protocol.py` | Interface for internal modules |
| `ExternalProgramAdapter` | `src/genesis/modules/external/adapter.py` | Bridge for external programs |
| `HttpIPCAdapter` | `src/genesis/modules/external/ipc.py` | HTTP-based IPC for external programs |
| `StdioIPCAdapter` | `src/genesis/modules/external/ipc.py` | Stdio-based IPC alternative |
| `ProgramConfig` | `src/genesis/modules/external/config.py` | YAML config for external modules |
| Module configs | `config/modules/*.yaml` | Per-module configuration files |
| `ModuleRegistry` | `src/genesis/modules/registry.py` | Central registry and lifecycle |

---

## Relationship to Other Architecture Docs

- **Capability Layer Addendum** (`genesis-v3-capability-layer-addendum.md`):
  Describes the strategic vision for Genesis as "single pane of glass."
  This document defines how individual capabilities (modules) plug in
  without compromising the cognitive core.

- **V5 Architecture** (`genesis-v5-architecture.md`): Describes
  self-evolution and graduated trust. The module paradigm is the
  boundary that keeps self-modification (V5) separate from module
  operations — modules evolve independently of Genesis's cognitive
  architecture.

- **Design Principles** (CLAUDE.md): "Cognitive architecture is not a
  service" — this document is the enforcement specification for that
  principle at the module layer.
