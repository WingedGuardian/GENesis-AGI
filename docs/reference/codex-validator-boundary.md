# Codex validator action boundary

`scripts/hooks/codex-validator-guard` is the native PreToolUse wrapper for a
separately configured external validator workspace. This foundation does not
install a hook or activate a validator session.

The launch definition supplies absolute runtime and workspace roots. Payload
cwd, role, session and transcript fields supply no authority. The dispatcher
uses the shared validator MCP profile and exact server prefixes. Shell actions
are refused until their separate mutation policy is installed. Patches admit
every potential source and move destination inside the trusted workspace;
existing symlinks, special files, hardlinked leaves, parent traversal, and
instruction/configuration paths (`AGENTS.md`, `.codex`, `.agents`, `.git`) are
refused. The conservative native 0.161.0 subset supports LF/CRLF and ordinary
Unicode filenames, while refusing heredoc envelopes, URI/backslash filenames,
environment selectors and multiple envelopes. Header-looking context can
cause an extra path check or conservative refusal. Native chunk validation
still follows admission; the guard never executes a patch itself.
Malformed inputs and unsuccessful evaluator results produce explicit denials;
native allows have empty stdout.

This is bounded accident prevention under full access. It does not isolate
arbitrary programs, later stdin, filesystem races, disabled hooks or failures of the outer hook
process. Activation requires the later workspace and readiness checks, including
vetted script bytes, a launch-owned interpreter with the required dependencies,
timeout margin above the measured cold import, and generated tool lists derived
from the shared profile.

Focused checks: `pytest tests/test_hooks/test_codex_validator_guard.py tests/test_hooks/test_codex_validator_patch.py`. Native
CLI/app checks in the same file require `GENESIS_CODEX_NATIVE_TESTS=1` and use
the isolated fake Responses provider and fake MCP from the native harness.
