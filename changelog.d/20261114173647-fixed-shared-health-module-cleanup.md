- Opt-in shared health frontends retain local module resources through cancellation and shutdown, retire abandoned stdio sessions before reuse, and report unconfirmed cleanup before closing dependencies. Normal execution limits and the default-off rollout remain unchanged.

Readiness and IPC use an owned async task so an owner stop preserves the MCP connection after previously handled cancellation, while real caller cancellation still propagates after bounded cleanup.
