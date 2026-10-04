- **Optional graph engine: module fetched, service unit written, nothing armed.**
  `bootstrap.sh` now downloads the FalkorDB engine module (one file under
  `~/.genesis/deps`, checked against a pinned digest and re-checked on every
  run) and writes a `genesis-falkordb` user unit that stays **disabled**: it
  listens on no TCP port, only a unix socket in `~/.genesis/falkordb`, and can
  write nothing in your home but its own data directory where the host allows
  user-unit sandboxing. Nothing on your system changes — the `redis-server`
  8.x the engine needs is a manual, opt-in step described in SETUP.md, "Graph
  engine (FalkorDB)". Once you arm the unit, the infrastructure health check
  alerts if it fails to start or loses its socket; an engine you never armed
  stays silent. `GENESIS_FALKORDB_PROVISION_DISABLED=1` skips the module
  download.
