# Codex validator action boundary

`scripts/hooks/codex-validator-guard` is the native PreToolUse wrapper for a
separately configured external validator workspace. This foundation does not
install a hook or activate a validator session.

The launch definition supplies absolute runtime and workspace roots. Payload
cwd, role, session and transcript fields supply no authority. The dispatcher
uses the shared validator MCP profile and exact server prefixes. Shell and
patch actions are refused until their separate mutation policies are installed.
Malformed inputs and unsuccessful evaluator results produce explicit denials;
native allows have empty stdout.

This is bounded accident prevention under full access. It does not isolate
arbitrary programs, later stdin, disabled hooks or failures of the outer hook
process. Activation requires the later workspace and readiness checks, including
vetted script bytes, a launch-owned interpreter with the required dependencies,
timeout margin above the measured cold import, and generated tool lists derived
from the shared profile.

The interpreter must also run with an environment the launch controls: no
inherited `PYTHONPATH`, `PYTHONHOME`, or user-site and `sitecustomize` code,
since any of them can run before the guard and print its result. Names that
must match the activated configuration exactly:

- MCP server keys `genesis_validator_health` and `genesis_validator_memory`
  (tool names `mcp__<server>__<tool>`, `MCP_SERVER_PREFIXES` in
  `codex_validator_guard.py`). The in-tree Codex configuration names its servers
  `genesis-health` and `genesis-memory`, which this guard denies.
- Profile servers `health` and `memory` and `ExternalProfile.VALIDATOR` in
  `src/genesis/mcp/external_profiles.py`.
- Codex tool names `Bash` and `apply_patch`, each carrying `tool_input.command`.

Focused checks: `pytest tests/test_hooks/test_codex_validator_guard.py`. Native
CLI/app checks in the same file require `GENESIS_CODEX_NATIVE_TESTS=1` and use
the isolated fake Responses provider and fake MCP from the native harness.
