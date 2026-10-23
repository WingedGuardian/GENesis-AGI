# GitNexus in Codex

The trusted project `.codex/config.toml` connects `gitnexus` using
`bash .claude/mcp/run-gitnexus mcp` from the workspace. The launcher reuses
`scripts/lib/gitnexus_version.sh`: it requires the installed pinned package and
supported Node runtime, and refuses missing, conflicting or mismatched installs.
It does not download a package or rebuild the repository index at startup.

The project explicitly admits 15 tools. A future tool added upstream is not
automatically admitted. Source-editing `rename` and group-metadata `group_sync`
are excluded. Raw Cypher and group listing remain available. This catalog
restriction does not make GitNexus side-effect-free: retained operations can
recover/cache data, and it does not constrain a separate raw server or host
operator. Tool availability still requires task authorization. The Genesis health and memory entries
retain their existing narrow allowlists. Interactive Genesis writer activation
is separate work; this connection does not enroll Codex in Genesis's lifecycle.

Codex supports project MCP configuration for trusted projects, `cwd`, startup
timeouts and tool allowlists. See the [official MCP reference](https://learn.chatgpt.com/docs/extend/mcp?surface=cli).
The default tool timeout remains unchanged. A same-name project transport can
replace a user-level command and argument list; user-level per-server environment
settings can still merge. This configuration is not an environment sandbox.

GitNexus is snapshot-based. Check its reported commit and staleness before
using results for a current-code claim. Follow the repository's code-intelligence
guidance for index refresh; linked worktrees use live symbol tools rather than
starting another index build. This connection does not certify index freshness.

For startup failures, run `.claude/mcp/run-gitnexus --version` from the checkout
and read its refusal diagnostic. Correct the installation or Node selection
through the existing GitNexus setup path; do not replace the launcher with an
unpinned `npx` command. An optional server failing to start does not make its
tools available merely because Codex itself started.

GitNexus remains optional for project startup. In the Codex 0.163.0 native
qualification, an immediate scripted first CLI turn did not expose the optional
server's tools. Jobs that need GitNexus on that first turn should explicitly
require it:

```bash
codex -c mcp_servers.gitnexus.required=true exec 'YOUR TASK'
```

This override makes GitNexus startup a requirement for that invocation, so a
missing or mismatched installation can prevent it from starting. The native
tests exercise this CLI override, a required disposable app-server profile,
and an optional app-server connection and direct tool call after startup.

The opt-in `tests/test_hooks/test_codex_gitnexus_native.py` qualifies installed
Codex CLI and app-server binaries with the real pinned GitNexus package, copied
project sources, a private registry, and a localhost fixture model provider.
It checks conflicting user configuration, both excluded mutators, an independent
excluded-list_repos control, and disabled-tool refusals on direct app-server calls. No paid
model or production Genesis MCP bootstrap is used. Qualification applies to
the recorded binaries and sources; it does not certify every graph/edit tool,
desktop UI behavior or production data.
