# MCP process observations

`scripts/mcp_process_report.py` is a read-only Linux diagnostic. It does not
change client configuration, query session transcripts, or signal processes.
Run it with the standard library Python; Genesis startup is not required.

```bash
python3 scripts/mcp_process_report.py
```

Save output in a new private evidence file outside the repository. Preserve
earlier snapshots. `--cgroup /sys/fs/cgroup/<explicit-scope>` optionally records
that cgroup's current/max/swap/pressure values; the report does not infer that
the cgroup belongs exclusively to the selected processes. `--proc-root` is for
fixtures or explicitly mounted proc views.

The report selects recognized MCP launcher forms and descendants of recognized
Codex app-server processes. This is a bounded discovery heuristic, not an
inventory of every possible MCP executable or wrapper. It omits raw arguments,
environment variables and transcript contents. Linux `/proc` is not an atomic
snapshot: start ticks and parents are rechecked after memory collection;
unstable observations lose both ownership and memory claims. A stable parent
chain means `observed_ancestry`, not proof of a conversation's ownership or
whether a process can be stopped. Boot ID, PID and start ticks identify each
observed process. Parent PID or creation-time similarity alone is insufficient.

RSS, PSS and SwapPss are separate KiB measurements. Totals include only available
measurements and expose measured/selected counts; unavailable values remain
`null`. RSS accounting can be imprecise; PSS is proportional attribution, not a
promise about reclaimed memory. See the kernel's
[/proc documentation](https://docs.kernel.org/filesystems/proc.html).
Discovery errors and memory errors remain visible. A scan without recorded
errors still cannot account for new, hidden or unrecognized processes.

Conversation ownership stays `unknown`. This proc-only command reports thread
inventory as `unavailable`; it does not convert API failure to an empty list or
join threads to children by creation time. If separately collecting a supported
thread API inventory, paginate it and record the API version, collection time,
last cursor and any unavailable/truncated result. Keep that evidence private;
do not publish thread titles or transcripts in a PR. Neither age nor low CPU
establishes a leak or authorizes process cleanup.

## Stopping a shared Codex daemon's MCP servers

The report observes; it never stops anything. The remedy is a separate tool
that the Sentinel proposes during a container memory alarm, and it runs only
when the owner approves the proposed action:

```bash
python -m genesis.sentinel.codex_mcp_stop            # dry-run: list targets, signal nothing
python -m genesis.sentinel.codex_mcp_stop --execute  # only inside an approved Sentinel action
```

- **Targets are resolved when the tool runs.** A target is a direct child of a
  `codex app-server --listen` (shared daemon) process that leads its own process
  group and talks over pipes on both stdin and stdout.
- **Excluded:** Codex's own helper binaries, a chat command (Codex runs those in
  a terminal or with stdin set to `/dev/null`), systemd-unit members, and
  anything under a Claude Code session. Anything the tool cannot read is skipped
  and counted.
- **How it stops them:** each group gets SIGTERM, then SIGKILL after a 5 s grace.
  Codex does not respawn a killed stdio server, so threads on that daemon lose
  those tools until they are reopened.
- **Where the build-up happens:** the daemon path, which editor clients such as
  the VS Code extension use. A terminal `codex` runs its servers in-process
  with the TUI, and its subagents share one set.
- **Turning it off:** `codex_mcp_stop.mode: off` in `config/sentinel.local.yaml`,
  or `GENESIS_CODEX_MCP_STOP_DISABLED=1`, makes the tool refuse before scanning
  (exit 4), in the dry-run and with `--execute` alike.
