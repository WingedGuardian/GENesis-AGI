- **The graph engine's projection now refreshes on its own, hourly.** If you
  armed the graph engine, its projection of the memory graph used to be a
  deliberate act — you ran `python -m genesis.memory.graphstore_project` by hand,
  and from that moment the projection drifted from the real graph until you ran
  it again. Nothing announced the drift, because a stale projection and a current
  one are indistinguishable from the engine's side: it serves links you have
  since removed, omits links you have since added, and never errors.

  MEASURED on a live install, six days after one manual projection: **2,286 links
  and 659 memories missing.** The drift is also bursty rather than steady — one
  week on the same install ranged from 4,075 new links in a day down to 5 — so
  "it has probably not changed much" is not a safe assumption to run on.

  A new `genesis-graph-project.timer` now rebuilds it every hour. That bounds how
  far behind the projection can be, which is what makes moving reads onto the
  engine a decision you can take at any moment rather than one that lands on a
  graph as stale as the day you built it. A full rebuild MEASURED 16.1s against
  285,683 links, and it is built alongside the live copy and swapped in
  atomically, so nothing reading the graph sees a partial one.

  **This does not move your reads.** The selector in `config/graphstore.yaml`
  still defaults to the in-process NetworkX store. The projection is kept current
  whenever the engine *exists*, not only once reads point at it — deliberately,
  since a projection that only starts refreshing after you flip the lever gives
  you a stale graph at exactly the moment you begin trusting it.

  **If you never armed the graph engine, this does nothing at all.** The timer is
  installed everywhere, so it also runs on installs that have no engine — there
  it exits immediately with one line and no error, rather than failing hourly
  forever. Setting `enabled: false` in `config/graphstore.yaml` stops it too.

  Still outstanding, and worth knowing if you are relying on this: an hour is a
  *bound*, not freshness-on-write. A burst inside the hour can still leave the
  projection meaningfully behind. Write-level freshness needs a database-side
  change signal, which is tracked separately and is not part of this change.
