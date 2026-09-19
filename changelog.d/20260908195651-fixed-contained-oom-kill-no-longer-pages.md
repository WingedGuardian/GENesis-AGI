- **An out-of-memory kill inside a deliberately capped job no longer pages as a
  container emergency.** Some background jobs run under their own memory cap on
  purpose, so a runaway one is killed without touching anything else — that is
  the containment working. But the kill counter the monitor watches aggregates
  every such event up to the container, so a contained kill paged as "processes
  OOM-killed in the container" and pointed at your sessions as the likely
  victims, while the container itself had gigabytes free. One stuck indexing
  job produced eleven emergency pages in two hours this way.

  The monitor now asks the system journal which unit actually died before
  paging. A kill fully accounted for by known capped jobs is recorded in the
  log and the durable snapshot but does not page; anything else — an unknown
  unit, no record, or no journal to ask — pages exactly as before, now naming
  the killed unit when it is known. The set of "known capped jobs" is
  configurable. Attribution can only ever downgrade an explained kill, never
  silence an unexplained one.
