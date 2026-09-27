- **The recursive-delete safety guard now judges a wildcard by the folder it
  sits in.** Deleting `<a>/<b>/<c>/*` removes everything deleting `<a>/<b>/<c>`
  would, but the depth floor counted the `*` as a fourth path level. So it
  refused the folder and allowed its entire contents: `~/tmp/*` or `~/tmp/tmp*/`
  in a scratch area shared by every session, for example. The floor now applies
  to the literal prefix before the first `*`, `?`, `[` or extglob pattern.
  A wildcard whose literal prefix is at least four levels deep (for example
  `/home/<user>/tmp/<job>/*`) is unaffected. A dot-wildcard in the middle of a
  path (`…/.?/…`), which can match `..` and climb upward, is refused. Measured
  over 1,617 recorded delete commands on one install: 20 (1.24%) are newly
  refused, all wildcard cleanups directly under a shared scratch folder, and
  none is newly allowed. Keep per-task scratch in its own subfolder so a
  wildcard cleanup stays at least four levels deep.
