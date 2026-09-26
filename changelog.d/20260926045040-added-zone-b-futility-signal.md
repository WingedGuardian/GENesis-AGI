- **A `/tmp` cleanup that reclaims nothing now says so, and names what is
  holding the space.** The watchdog's `/tmp` tier chose its severity from a
  reading taken *before* its own cleanup and then never looked again — so a
  sweep that freed nothing was indistinguishable from a sweep that had nothing
  to do, and the tier simply re-entered every 30 seconds. Measured on a live
  install: over 1,250 consecutive passes across four days, each reclaiming zero
  bytes, with nothing in the log saying so. The cause was ownership. `/tmp` is
  a sticky directory, where only a file's owner may remove it, so most of the
  space in use belonged to another account and no number of retries could ever
  have touched it — and every sweep ends in a form that discards its errors,
  which made a permission failure look exactly like a clean run.
  The tier now re-measures after cleaning and, when nothing moved and it is
  still over its line, records once how much it cannot reclaim and which
  accounts own it. Three details earn their keep. A tree it is not allowed to
  *read* is reported as an owner found but not sized, rather than as no owner
  at all — that shape is the common one (10 of the entries on the measured
  install) and collapsing it would make the report state the opposite of what
  it found. The record is kept per severity, so a tier climbing from one level
  to the next is not silenced by the record the milder level already wrote.
  And directories the sweeps deliberately spare are left out of the
  attribution, because they survive for a reason that has nothing to do with
  who owns them, and blaming ownership would send the reader to the wrong fix.
  It reports only at the two upper tiers: reclaiming nothing at the lowest one
  is the healthy steady state for a filesystem holding live content, and a
  check that cries wolf gets silenced. No paging, no change to what is deleted;
  breaking the futile loop itself waits for the matching work on the other
  zone, so the two designs land together rather than diverging.
