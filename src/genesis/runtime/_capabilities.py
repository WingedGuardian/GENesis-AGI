"""Capabilities manifest writer — ``~/.genesis/capabilities.json``.

The SessionStart hook reads ``capabilities.json`` to tell foreground
CC sessions which subsystems are active, degraded, or failed. This
module owns the dict of human-readable capability descriptions and
the atomic write to disk.

Extracted out of ``_core.py`` because this is a one-shot operation
run from the tail of ``bootstrap()``, not an ongoing behavior of the
runtime instance. Free function taking the runtime as its first arg
is the cleanest shape — it reads ``runtime._bootstrap_mode``,
``runtime._bootstrap_manifest``, and ``runtime._module_registry``
and writes the file once.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from genesis.runtime._core import GenesisRuntime

logger = logging.getLogger("genesis.runtime")


_CAPABILITY_DESCRIPTIONS: dict[str, str] = {
    "secrets": "API key loader for external services (Gemini, Groq, Mistral, etc.)",
    "db": "SQLite database (60+ tables) — use db_schema MCP tool to discover tables and columns before querying",
    "tool_registry": "Registry of known tools available to CC sessions",
    "observability": "Event bus, structured logging, and provider activity tracking",
    "providers": "Provider registry — web search, STT, TTS, embeddings, health probes, research orchestrator",
    "awareness": "Awareness loop — periodic signal collection ticks driving system health and perception",
    "router": "LLM routing with circuit breakers, cost tracking, and dead-letter queue",
    "perception": "Reflection engine — observation creation, pattern detection, signal processing",
    "cc_relay": "Claude Code invoker, session manager, checkpoints, and reflection bridge",
    "memory": "Hybrid memory store — SQLite + Qdrant vector search, wing/room taxonomy, essential knowledge layer",
    "surplus": "Surplus compute scheduler — uses idle time for brainstorms and enrichment tasks",
    "learning": "Learning pipeline — triage, calibration, harvest, and procedural learning",
    "inbox": "Inbox monitor — watches ~/inbox/ for markdown files with URLs, evaluates them in background CC sessions",
    "mail": "Mail monitor — polls Gmail inbox weekly, two-layer triage (Gemini + CC), stores recon findings",
    "reflection": "Reflection scheduler — deep and light cognitive reflection cycles",
    "health_data": "Health data service — aggregates subsystem status for dashboard and MCP tools",
    "outreach": "Outreach pipeline + scheduler — morning reports, alerts, proactive Telegram messages",
    "autonomy": "Autonomy manager — task classification, protected paths, action verification, approval gates",
    "modules": "Capability module registry — domain-specific add-on modules (prediction markets, crypto ops)",
    "pipeline": "Pipeline orchestrator — signal collection, triage, and module dispatch cycles",
    "memory_extraction": "Periodic cross-session memory extraction — entities, decisions, relationships from conversation transcripts",
    "tasks": "Task executor — autonomous multi-step task execution with adversarial review, pause/resume/cancel",
    "guardian": "External host VM guardian — container health monitoring, diagnosis, and recovery",
    "guardian_monitoring": "Guardian bidirectional monitoring — detects stale Guardian heartbeat and auto-restarts via SSH",
    "sentinel": "Container-side guardian — autonomous CC call site for infrastructure diagnosis and remediation, counterpart to external Guardian",
    "codebase_index": "AST-based codebase structural index — modules, symbols, imports stored in SQLite for code-aware sessions",
}


_MODULE_DESCRIPTIONS: dict[str, str] = {
    "prediction_markets": "Prediction market analysis — calibration-driven forecasting, market scanning, Kelly position sizing",
    "crypto_ops": "Crypto token operations — narrative detection, launch monitoring, position health tracking",
    "content_pipeline": "Content pipeline — idea capture, weekly planning, voice-calibrated script drafting, multi-platform publishing, analytics feedback loop",
}


def write_capabilities_file(runtime: GenesisRuntime) -> None:
    """Write ``~/.genesis/capabilities.json`` based on the bootstrap manifest.

    Called once from ``GenesisRuntime.bootstrap()`` at the tail of
    initialization. Uses an atomic ``tempfile.mkstemp`` + ``os.replace``
    to avoid leaving a half-written file if the process dies mid-write.

    In ``readonly`` bootstrap mode, existing active capabilities take
    precedence over newly-degraded ones so a readonly probe doesn't
    overwrite the primary runtime's healthy state.
    """
    capabilities: dict[str, dict[str, str]] = {}

    existing: dict[str, dict[str, str]] = {}
    if getattr(runtime, "_bootstrap_mode", "") == "readonly":
        cap_file = Path.home() / ".genesis" / "capabilities.json"
        if cap_file.exists():
            try:
                existing = json.loads(cap_file.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning("Failed to read existing capabilities.json: %s", exc)

    for name, raw_status in runtime._bootstrap_manifest.items():
        desc = _CAPABILITY_DESCRIPTIONS.get(name, name)
        if raw_status == "ok":
            capabilities[name] = {"status": "active", "description": desc}
        elif raw_status == "degraded":
            capabilities[name] = {"status": "degraded", "description": desc}
        else:
            error_msg = raw_status.removeprefix("failed: ") if raw_status.startswith("failed:") else raw_status
            capabilities[name] = {
                "status": "failed",
                "description": desc,
                "error": error_msg,
            }

    if runtime._module_registry:
        for mod_name in runtime._module_registry.list_modules():
            mod = runtime._module_registry.get(mod_name)
            if mod:
                capabilities[f"module:{mod_name}"] = {
                    "status": "active" if mod.enabled else "disabled",
                    "description": _MODULE_DESCRIPTIONS.get(mod_name, mod_name),
                }

    if existing:
        for name, info in existing.items():
            if name not in capabilities or (
                info.get("status") == "active"
                and capabilities[name].get("status") == "degraded"
            ):
                capabilities[name] = info

    cap_file = Path.home() / ".genesis" / "capabilities.json"
    cap_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=cap_file.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(capabilities, f, indent=2)
            os.replace(tmp_path, cap_file)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
        logger.info("Capabilities file written: %d entries", len(capabilities))
    except OSError:
        logger.error("Failed to write capabilities file", exc_info=True)
