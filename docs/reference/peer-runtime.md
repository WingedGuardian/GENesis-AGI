# Installed peer runtime

The standalone host calls `GenesisRuntime._init_peers()` after full bootstrap,
before creating its Flask app. It owns one private `PeerRuntime`, coordinator,
Unix broker and five-second poller. Generic foreground and bridge bootstraps do
not install another peer controller. Existing server process locking, Flask
bindings and Tailscale proxy settings remain unchanged.

Installation uses the actual initialized SQLite database, discovered through
`PRAGMA database_list`, with private registry connections. Persistent migrated
storage is required. The service user owns mode0700 directories under
`genesis_home()/peers`; each boot creates a new private broker directory and
socket instead of reusing or unlinking an old socket.

Recovery precedes execution prerequisites and runs even when peer mode is
disabled or critical bootstrap failed. It visits unfinished segments and
nonterminal tasks, including already-drained segments awaiting disposition.
Exact named scopes must drain before capacity is released. A prepared segment
with no committed start is proven unstarted and charges zero; running work with
unknown elapsed time charges its original reservation. Repeated settlement
never refunds or double-charges work. Executing consequential receipts become
unknown and retain reconciliation; restart does not authorize replay.

Recovery preserves exact approval/provider holds and consumed continuations.
A successful recorded session can publish only through the normal owned-result
publisher, with exact task/segment/generation, completed peer session, explicit
clean and noncancelled outcome, private full artifact and valid timing proof.
Missing successful proof fails interrupted work rather than inventing output
from truncated metadata. Unconfirmed cleanup blocks peer readiness.

Execution requires bootstrapped runtime, shared direct runner/session manager,
ready background autonomy, approval manager and normal approval gate. Mode,
pause, readiness and shared runner capacity are checked before admission and
claim; start, approval consumption and provider resume recheck the host gate.
Housekeeping and cancellation continue while execution is paused or disabled.
Readiness loss leaves the HTTP objects bound but refuses new submissions.

The host binds its registry, runtime owner, results and approvals into the
existing peer blueprint. Authenticated health reports peer active-slot and
pending-queue counts; the card advertises conversation and installed research while execution is
available. These objects are internal Flask configuration, not environment
switches. No machine approval resolver is added.

Before Telegram closes, peer shutdown refuses admission, fences leases and
cancels polling and existing notification deliveries. Session drain runs as an
owned task with the existing ten-second host grace. Pending cleanup is recorded
as blocked/reconciliation; it is never reported clean or given new capacity.
Exceptions in peer teardown do not prevent unrelated runtime shutdown.
Loss of the poller also fences active leases and cancels their running sessions;
readiness refuses new work until host recovery. Awaited drain and reconciliation
remain owned by runner cleanup and shutdown.

The runtime installs [research](peer-research.md) before broker startup and owns
the lazy search client. Client retirement follows successful coordinator drain,
including a close task that remains pending beyond the shutdown grace.

Operators enable a relationship through the local
[peer registry CLI](peer-registry.md), configure distinct credentials without
putting their values in chat, and restart through the approved deploy scripts.
Clearing a credential revokes it; disabled mode stops new execution while still
recovering old scopes at startup. A blocked scope/effect requires operator
reconciliation, not a restart that resets budgets or approval evidence.

Tests use a private SQLite database, real runtime initializer, poller, runner,
approval manager, outreach pipeline, Unix broker, stdio facade and authenticated
HTTP/dashboard routes. External model invocation, systemd drain and Telegram
transport are fixture adapters. This does not establish a production deploy,
live provider invocation or tailnet acceptance.
