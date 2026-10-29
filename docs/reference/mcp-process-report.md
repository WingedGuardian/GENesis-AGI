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
