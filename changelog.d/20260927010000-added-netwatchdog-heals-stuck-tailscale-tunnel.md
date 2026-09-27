- **The network watchdog now heals a stuck Tailscale tunnel.** When a peer you
  are actively using stops completing WireGuard handshakes for over five
  minutes, while that peer still answers discovery pings (so the path is fine
  and only the tunnel is dead), the watchdog restarts `tailscaled`, records
  what it saw, and sends you an alert. This is the failure where Tailscale SSH
  sessions suddenly time out while everything else looks healthy. The restart
  drops every Tailscale SSH session on the machine (tmux sessions survive), so
  it happens at most once an hour. It never starts a stopped `tailscaled`. Set
  `NETWD_TS_MODE=observe` on `genesis-network-watchdog.service` to get the
  alert without the restart, or `off` to disable it. Runs wherever the network
  watchdog is installed (systemd-networkd hosts), and takes effect on the next
  `update.sh`.
