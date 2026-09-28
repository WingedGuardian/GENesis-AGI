- **The network watchdog now heals a stuck Tailscale tunnel.** When a peer you
  are actively using stops completing WireGuard handshakes for over five
  minutes, while that peer still answers discovery pings (so the path is fine
  and only the tunnel is dead), the watchdog restarts `tailscaled`, records
  what it saw, and tells you. This is the failure where Tailscale SSH
  sessions suddenly time out while everything else looks healthy. The restart
  drops every Tailscale SSH session on the machine (tmux sessions survive), so
  it happens at most once an hour. It never starts a stopped `tailscaled`. Set
  `NETWD_TS_MODE=observe` on `genesis-network-watchdog.service` to be told
  without the restart, or `off` to disable it. A failed restart (Tailscale may
  be down) or an observe-mode detection pages you on Telegram; a successful
  heal appears on the dashboard and in the morning report. The watchdog runs as
  root but writes only its own `/run` telemetry; the Genesis runtime reads each
  event from there and records it as an observation, so nothing root-owned is
  written into your home directory. Runs wherever the network watchdog is installed
  (systemd-networkd hosts), and takes effect on the next `update.sh` plus a
  Genesis server restart.
