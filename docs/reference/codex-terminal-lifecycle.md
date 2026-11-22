# Native terminal enrollment qualification

Codex CLI 0.171.0 on Linux x86-64 did not pass automatic enrollment for ordinary
Genesis terminals. The executable SHA-256 was
`b76a027d6b210f9a8da7aaf321dd235c700f0ef7a794d4a5314b9af6c134f0f0`.

The qualification used an isolated Codex home, a loopback fixture model,
a private tmux server, synthetic prompts and trusted fixture hook definitions.
The decision rule required distinguishing an owner's terminal from a
Genesis-dispatched process and an independent app-server client, including
when the local daemon was already running.

| Control | Rollout source | Originator | Hook ancestor | Terminal ancestor | Dispatch marker in hook |
|---|---|---|---|---|---|
| Ordinary terminal | vscode | codex-tui | app-server | absent | absent |
| Terminal started with dispatch marker | vscode | codex-tui | app-server | absent | absent |
| Independent app-server client using the same client name | vscode | codex-tui | app-server | absent | absent |

The two terminal controls shared a prestarted daemon. The independent client
used its own app-server over stdio. Both terminals recorded the same
client-supplied thread source (`user`). The hook session ID matched its
rollout in all three cases. In an embedded
terminal the hook could see the terminal ancestor and inherited dispatch marker.
That observation did not carry over to the warm daemon. A terminal ancestor
of a cold daemon can also belong to the process that started it.

Startup, exact resume, fork, and manual compaction context reached the fixture
model. Exact resume retained the thread ID and fork created a distinct one.
A clean exit emitted SessionEnd. A process killed with SIGKILL produced no
SessionEnd within the observation window. Separate controls observed automatic
compaction with a subsequent model request and native subagent start and stop.

Keep automatic registry writes, ledger injection, and peer delivery disabled
until a per-thread signal binds the current terminal and its dispatch role.
Repeat the three warm controls after a harness change. Positive lifecycle
events are insufficient to establish who owns the thread.

A separate idle-thread control verified that app-server `turn/start` accepts
empty user input and a named `toolOutput`. The fixture model received one
`function_call_output`, and thread readback preserved the namespace, name
and body. These are caller-supplied fields. A future Genesis relay must look
up the sender itself and bind the recipient's current enrollment before
using this transport. This control used one isolated Codex thread.

Tracking: [Codex session lifecycle, #4156](https://github.com/WingedGuardian/GENesis-AGI/issues/4156).
