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

## Managed Codex lifecycle inventory

`python -m genesis.sentinel.codex_mcp_inventory` reads the current local
managed daemon through its standard Unix control socket under Codex home.
The collector opens the socket directly and reads the version from the
initialization response, so polling cannot start the CLI's update wrapper.
It initializes as Codex's non-originating backend client to preserve the
active client's process-wide identity. It checks
the peer PID and UID against the process scan, enumerates loaded threads
with pagination, and aggregates metadata by source and activity. It refreshes
the loaded set before accepting the sample. The command prints only the
aggregate and never subscribes to threads or opens a conversation.

The learning scheduler takes one sample per minute. Daily event pruning applies a
72-hour cutoff to `codex.mcp_inventory` events. The evidence
contains only daemon identity and version, counts, held memory (RSS plus
swap), and proportional memory when readable. Group counts and memory cover
the stop-eligible MCP groups. A complete sample reflects a complete read of
that scope. Unavailable, absent and partial outcomes are recorded to make
sample coverage visible. A process scan and a thread report are
separate observations, so a process group cannot be attributed to a
conversation. For a controlled before/after comparison, ensure the client
actually unsubscribes and measure again after its configured unload delay.
Check the terminal and editor clients separately because their transport
can change with Codex versions and startup configuration.
The opt-in native test
`test_unsubscribe_unloads_the_thread_and_reaps_its_stdio_child` exercises a
disposable backend with a one-second unload delay. It verifies that the thread
leaves the loaded inventory and the exact responding MCP child exits while the
backend stays open. It covers the protocol release path for the fixture.

Set `GENESIS_CODEX_MCP_INVENTORY_DISABLED=1` to disable periodic collection.
The supervisor collects no samples while `codex_mcp_stop.mode` is off.

### Shadow paging evidence

The threshold alert and activation rule continue to run while inventory
events record both a legacy threshold comparison and a shadow evidence verdict.
The threshold comparison uses all managed processes in the sample. The shadow
verdict uses the daemon verified through the control socket. Each event
records the configured threshold used for both comparisons. An unavailable
sample is recorded even when its daemon identity cannot be verified.
Observe at least 48 hours of shadow results before considering an enforcement change.
Include normal editor and terminal release, a threshold crossing, incomplete
samples, and an independently firing container-pressure alarm in the assessment.

The shadow verdict requires the sampled daemon's held-memory threshold and
two consecutive growth cycles on the same daemon. Each cycle expands
beyond the earlier loaded-thread set, returns to that set, and retains new
process groups seen during expansion. Genesis waits at least 60 seconds after
that return and confirms the surviving groups in two observations. Held
memory must still meet the threshold after unloading. This
comparison detects persistent growth associated with observed release cycles.
An incomplete sample, an interrupted return, or a gap of more than two minutes
breaks the streak. Groups already seen before expansion do not count as
growth. A later baseline reading below the threshold takes precedence over
an earlier higher confirmation.
The sample can associate growth with a daemon's thread inventory. It cannot
assign a process group to an individual conversation.

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
- **Where the build-up happens:** the shared daemon, which editor clients such
  as the VS Code extension use. Terminal Codex can run embedded or use the
  daemon depending on its version and launch path. Check observed ancestry
  before attributing held memory to a particular client.
- **Turning it off:** `codex_mcp_stop.mode: off` in `config/sentinel.local.yaml`,
  or `GENESIS_CODEX_MCP_STOP_DISABLED=1`, makes the tool refuse before scanning
  (exit 4), in the dry-run and with `--execute` alike.
