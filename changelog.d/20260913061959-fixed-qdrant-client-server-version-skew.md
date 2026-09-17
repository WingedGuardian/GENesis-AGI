- **The Qdrant client is now pinned to a version the bundled Qdrant server
  actually supports.** The client dependency carried no version constraint at
  all, while the installer defaults to installing server 1.14.0 when none is
  present — so a fresh install pulled whatever client was newest that day and
  paired it with a server three releases behind. Qdrant's own rule is that the
  two must share a major version and differ by at most one minor, so every client
  start logged an incompatibility warning.

  That drift was not only cosmetic. The 1.16 client removed a search method the
  memory near-duplicate check still called, so on any install that had picked up
  a 1.16-or-newer client, that check could only fail. It was not reached from
  anywhere in the running system, which is the sole reason nobody saw it — and
  also why no test caught it. That call now goes through the same query path the
  rest of the memory system uses, so it no longer depends on a removed method,
  and the migration is covered by tests.

  Two limits worth stating plainly. The new tests cover the migration and the
  filtering around it, not the check as a whole — it looks memories up by a
  payload key that most stored points do not carry, so it only ever examines a
  small fraction of the store. That is a longer-standing problem, tracked
  separately and deliberately not changed here. And the pin is now enforced by a
  test that re-derives the client/server compatibility window from both files,
  so the two can no longer drift apart quietly.

  Nothing about your stored memories changes, and the server is untouched. If you
  had previously installed a newer client by hand, updating will move it back into
  the supported range.
