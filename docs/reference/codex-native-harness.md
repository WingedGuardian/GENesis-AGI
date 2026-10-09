# Native Codex hook harness

Run the opt-in integration file on a Unix host with Codex and the Genesis Python dependencies installed:

```bash
GENESIS_CODEX_NATIVE_TESTS=1 GENESIS_PYTEST_LOCK_WAIT=1 \
  python -m pytest tests/test_hooks/test_codex_native.py -q \
  -o tmp_path_retention_policy=all \
  --basetemp "$HOME/tmp/codex-native-harness-$$"
```

`GENESIS_CODEX_CLI_BIN` and `GENESIS_CODEX_APP_SERVER_BIN` can select different absolute binary paths. Both default to the installed `codex`. An opted-in run fails when a required binary is missing; ordinary test runs skip this file.

Each case creates an isolated home and workspace, starts a localhost Responses fixture provider, and requests one shell, patch, or fake MCP action. No production Genesis server, database, MCP configuration, credentials, or paid model is used. The fixture hook records its real native input and permits or denies the action. Tests check the actual file sink, including positive controls for each denial case.

The 20 cases cross both clients with both decisions and five call paths: direct shell, direct patch, nested code-mode shell, nested code-mode patch, and nested code-mode MCP. The model name selects native tool behavior; all responses come from the local fixture provider.

Results apply to the recorded binaries and `gpt-6.1-sol` tool metadata. Analytics and plugins are disabled in the isolated config; the tests also reject evidence of a plugin-clone attempt. This is configuration-based suppression, not an OS network sandbox.

Activate the Genesis Python environment first. Both the unique `--basetemp` and retention override are required to retain passing and failing evidence: the repository otherwise removes successful cases and its automatic per-process temporary directory. Pytest clears an explicit base directory at startup, so use a new path for each run.

Hook trust is scoped to the vetted fixture: `hooks/list` supplies its exact key and current hash, which are persisted only in the isolated configuration. No hook-trust bypass flag is used. This confirms trust of the hook definition, not integrity of the referenced script bytes: a separate isolated Codex 0.161.0 probe kept the same current hash and trusted status after adding a harmless script comment. Production readiness must verify guard contents separately. `native-evidence.json` in each pytest temporary directory records binary versions, hook discovery, fixture requests, native events, and stderr. The fake MCP is marked required so startup must succeed before the initial tool catalog is built.

The app-server path drives initialization, an ephemeral thread, and a turn over stdio. It exercises the native backend; it does not certify desktop folder selection, the desktop trust UI, or production validator policy. Those require separate readiness checks. This harness also does not claim coverage of hook crashes, timeouts, later stdin input, or arbitrary indirect execution.
