- **Optional graph engine, opt-in and not armed.** Groundwork for FalkorDB, a
  graph engine for the memory graph. **Updating changes nothing on your system.**
  The part that would touch it -- adding Redis's official upstream apt repo and
  installing a `redis-server` new enough to load the engine module (it refuses
  anything below 8.0.0, and Ubuntu/Debian stable ship 7.x) -- is **opt-in**:

    ```bash
    GENESIS_FALKORDB_PROVISION=1 ./scripts/bootstrap.sh
    # or, persistently, graph_engine.provision: true in ~/.genesis/config/genesis.yaml
    ```

  Without that, bootstrap prints one line saying how to enable it and moves on.
  `GENESIS_FALKORDB_PROVISION_DISABLED=1` turns the whole thing off. The engine
  module itself (one file under `~/.genesis/deps`, no system change) is fetched
  either way, so arming the engine later is a single command.

  When the engine IS armed, the unit now reports itself ready rather than
  merely started: with the old `Type=simple`, systemd called it active as soon
  as the process forked, and the socket was measurably not answering yet (3 of
  3 restarts, 61-147ms). Anything checking "active" in that window saw a
  running engine with no socket, which is exactly the shape the health check
  treats as a fault. The interpreter path is also resolved when the unit is
  written instead of assuming `/usr/bin/redis-server`, so a box where the
  binary lives elsewhere no longer gets a unit that fails to execute with
  nothing explaining why. `genesis-server` now ORDERS itself after the engine —
  ordering only, deliberately without pulling it in, so an install that never
  opted in still never starts it. An armed engine that never becomes ready (a
  module that will not load, for instance) raises the infrastructure alert; an
  engine you never armed stays silent.

  Even with consent, every step declines rather than forces. **If you already
  run redis-server, Genesis leaves it and the apt repo completely alone** --
  adding the repo would upgrade your Redis on your next unrelated `apt upgrade`,
  which is not ours to do; you get printed instructions instead. No passwordless
  sudo, an unsupported architecture, an unrecognised distro, or a failed download
  each skip with a note and let bootstrap continue. The module is verified
  against a pinned digest, because upstream publishes no checksums of its own,
  and re-verified on every run; it is installed first, so a module that does not
  verify leaves no apt repo or daemon behind. The armed service can write only
  its own data directory -- the rest of your home is read-only to it.

  Nothing reads the engine yet, so its service unit is written but left
  **disabled** and the memory graph keeps using its in-process projection
  exactly as before. Arm it with
  `systemctl --user enable --now genesis-falkordb` once a later release wires
  a consumer.

  Two things worth knowing: the service listens on **no TCP port at all** (it
  speaks over a unix socket in `~/.genesis/falkordb`), and Redis 8 is
  tri-licensed under RSALv2/SSPLv1/AGPLv3 rather than the plain BSD of the 7.x
  in your distro -- running it unmodified places no obligation on your own code,
  but you are installing it, so you should know. Uninstall removes Genesis's own
  files; the `redis-server` package and the apt repo are deliberately left in
  place, since other software on the machine may rely on them.
