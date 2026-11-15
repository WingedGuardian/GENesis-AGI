# Persistent local live-agent host

The opt-in `genesis-live-agent.service` user unit runs `python -m genesis.live_agent`.
Normal install/update template rendering installs the unit file; it does not
enable or start this service. It uses the installed pinned Claude CLI and the
existing live-agent state file, so the text CLI and service cannot own it together.

After the service is deliberately activated, an owner terminal attaches with:

```sh
.venv/bin/python scripts/live_agent_attach.py
```

The endpoint defaults to `$XDG_RUNTIME_DIR/genesis-live-agent/control.sock`
(or `/run/user/<uid>/genesis-live-agent/control.sock`). The containing directory
must be owned by the user with mode 0700; the socket is mode 0600. Linux peer
credentials must match the host user. This trusts processes running as that
OS user; it is not speaker verification or an interface for model delegation.
No TCP/HTTP listener is provided.

One controller attaches at a time. `/s` shows status, `/i` interrupts, `/steer TEXT`
explicitly cancels pending asks and redirects, `/y ID` allows, `/n ID` denies,
and `/q` detaches. Other text sends a turn; it is held when an approval waits.
Only explicit approval IDs are supported, and snapshots show full tool inputs.
Detaching preserves the session; approvals still default to denial on expiry.

The local protocol is UTF-8 JSON lines. Every command carries a nonempty string
`id` and an `op`: `status`, `interrupt`, `turn`/`steer` with `text`, or `answer`
with `approval_id` and a JSON boolean `allow`. Responses echo the request ID.
Connections receive a status snapshot followed by live events. History is not
replayed. A slow controller is disconnected when its delivery queue fills;
reattach and inspect status before deciding whether to resend a request. Commands
are never retried automatically, because a lost response does not prove a command
was not executed.

The service declares speech output at identity injection. Unexpected SDK stream
loss closes the host with a failure exit for systemd recovery; intentional
SIGTERM/SIGINT closes it normally. Shutdown closes client connections before
waiting for the server, then disconnects the SDK and attempts confirmed-dead
registration checkpointing. Lock files are not unlinked; cleanup removes only
the socket inode this host bound. No new event journal or session store is added.
