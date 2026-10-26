- Transcript analytics queries without `live=True` now see the derived views only, whether or not a
  snapshot is current. When none is, the derived views are computed on a private connection and
  read in full, so raw views and reconciliation intermediates no longer leak through a cache
  fallback and the SQL text is no longer inspected. Snapshot reads skip a manifest or table that
  is not a regular file instead of blocking on it.
