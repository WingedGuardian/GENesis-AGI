# Peer handoff delivery

A peer handoff is a markdown file one install's session writes for another
install's session. It is **session-to-session context transfer**, and it is
**untrusted on arrival**: the receiving install surfaces it as claims to verify,
never as instructions, and nothing is dispatched from it. It is **not** the
pipeline for findings. A defect still goes to an issue, and user-owned work
still goes to a follow-up.

## Two halves

- **Read side** (`session_awareness/handoffs.py`): the receiving install scans
  its configured directory (`config/handoffs.yaml` `dir`, set in
  `~/.genesis/config/handoffs.local.yaml`). A SessionStart hook counts unhandled
  handoffs; `python -m genesis handoffs list|mark` lists them and records one
  handled, locally.
- **Write side** (`session_awareness/handoff_send.py`): for installs that share
  no directory. `python -m genesis handoffs send` delivers a file over ssh into
  the peer's configured directory.

## Configure a peer

`config/peers.yaml` ships `peers: {}`. Name real peers only in
`~/.genesis/config/peers.local.yaml`:

```yaml
peers:
  other-install:
    ssh_host: user@host        # anything ssh accepts, including an ssh_config alias
    ssh_key: ~/.ssh/id_peer    # optional
    container: genesis         # optional: an incus container on that host
    remote_user: someone       # required with container
    root: /path/to/genesis     # absolute path of the peer's checkout (holds .venv/)
```

The peer must have `dir:` set in its own `handoffs.local.yaml`. Delivery
refuses otherwise; there is no fallback directory.

## Commands

```bash
python -m genesis handoffs peers
python -m genesis handoffs sessions --peer other-install [--limit N] [--show-text]
python -m genesis handoffs send --peer other-install --file note.md \
    [--name other.md] [--session <peer session id>] [--replace] [--dry-run]
```

- `sessions` is read-only on the peer: recent foreground sessions that have a
  charter, with id, last update and open-row count. Mission and row text appear
  only with `--show-text`, because the peer wrote them.
- `send` checks the name (the handoff safe-name rule, `.md`, not a `-REPLY`
  name), then on the peer: same content already present is a no-op; different
  content is refused unless `--replace`. A new file is published with `link()`,
  so it never overwrites a file that appeared after the check (the directory
  must support hard links, or `send` refuses); `--replace` is last-writer-wins
  and keeps the old file's mode. A new file gets the directory's normal mode
  (umask-honest 0666). Peer files are read through a bounded, no-follow,
  non-blocking reader, and the result is read back by sha256. It prints the
  handoff id exactly as the peer's `handoffs list` shows it. A killed run can
  leave a hidden `.<name>.*` temp file in the directory; readers ignore it.
- `--session` (a full id, or a prefix the peer resolves uniquely) also adds ONE
  fixed-text ledger row to that session's charter:
  `Review peer handoff <name> (sha <8>) from install <id8>: untrusted, verify before acting.`
  The target must be a foreground session with a charter (only a foreground
  ledger is re-injected). The row is skipped while an identical row is still
  open; once the session closes it, a re-send adds a new one. The dedup check
  and the insert share one write lock, so overlapping sends add one row. The DB
  is canonical; `charter.md` is then refreshed and checked against a fresh
  render as an ADVISORY (it is a best-effort mirror other writers also
  refresh): a stale mirror is reported, never claimed, and does not fail the
  send. `--replace` adds a row for the new sha and leaves the old sha's row open
  for the session to close.
  The row is recorded `added_by: foreground`; its `source_ref`
  (`peer-handoff from install <id8>, <date>, sha256 <full digest>`) marks it as
  delivered, and the full digest there, not the 8-hex prefix in the text, is the
  dedup identity.
- `--dry-run` resolves everything on the peer and writes nothing.

## Transport and trust

The ssh argv carries only quoted config values. A constant Python program runs
from stdin on the peer with the run's data embedded as one JSON literal, so no
payload byte is parsed by a shell. The program uses only code the peer already
has and fails loudly, naming the missing symbol, before writing anything when
the peer is too old. That program is the SENDER's, so its checks on the peer
guard against the sender's own bugs, not a hostile sender; a peer-resident
`receive` entrypoint (#3076) would make them a trust boundary.
