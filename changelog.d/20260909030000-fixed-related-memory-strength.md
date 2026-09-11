- **Related memories now show how strongly they are actually connected, and the
  same question gives the same answer twice.** When a memory could be reached by
  more than one route of equal length, the graph walk credited it to whichever
  route it happened to look at first and reported THAT connection's strength —
  often the weaker one. It now uses the strongest connection that actually
  reaches it. Because strength is also what orders these results before the top
  few are shown, an understated memory sank in that ordering and could drop out
  of view.

  Measured against the live graph: of every connection the walk computes, 6.4%
  were understated, affecting half of all lookups — but most of those never reach
  you, so the number that matters is smaller. Of the related memories actually
  shown, the set changes on 1.9% of lookups and their order on 8.2%. The same
  change also removes a second oddity: which related memories you saw could shift
  between runs on identical data, purely from database row order — 3.1% of
  lookups before, none after. Nothing changes about which memories are
  *reachable*, and no connection is ever credited with a lower strength than
  before.

  The SQL path Genesis falls back to when the in-process graph is unavailable had
  the same flaw in a worse form: it could return the same memory twice, once with
  each route's strength, taking two of the handful of slots shown and dragging a
  false weaker number into the ordering. Fixing that exposed two more differences
  in the same place — the fallback would return a memory linked to itself as its
  own related memory (30 such links exist), and it answered with one level of
  results even when asked for none. Both are closed, so the two paths now agree
  on every case covered here: same memories, same strengths, same labels, same
  order. One difference is left standing deliberately, because it cannot occur
  with the identifiers Genesis actually uses: the fallback's loop protection
  compares identifiers as text, so it would over-prune if one memory's id were
  contained in another's.
