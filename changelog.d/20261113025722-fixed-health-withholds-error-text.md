- **Detail text on the health endpoint now needs dashboard sign-in.** A caller
  of `GET /api/genesis/health` with neither a signed-in dashboard session nor
  the internal API token gets every status, count, flag, id and timestamp, with
  `<field>_hidden: true` in place of each text field and `free_text_hidden:
  true` on the response. A text field added to the snapshot later is handled
  the same way. The host-side supervisor and the deploy scripts read only
  statuses and numbers and are unaffected, and the status code is decided
  before anything is withheld.

  On an install with no dashboard password nothing can sign in, so the
  dashboard header and both monitor pages show a note where that text used to
  be, from localhost too. Set a dashboard password (Configuration, Provider
  Keys) and sign in to see it again.
