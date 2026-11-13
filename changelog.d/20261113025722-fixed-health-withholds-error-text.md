- **The health endpoint no longer returns error or detail text to a caller
  without a credential.** `GET /api/genesis/health` answers every network, and
  besides call-site responses its body carried exception and provider error
  text, queue items' reasons and errors, MCP server errors, probe messages, the
  sentinel's trigger reason and memory tags, any of which can name paths, hosts
  or account details. A caller with neither a signed-in dashboard session nor
  the internal API token now gets every status, count, flag, id and timestamp,
  with `<field>_hidden: true` in place of each withheld text and
  `free_text_hidden: true` on the response. A text field added to the snapshot
  later is withheld the same way. The host-side supervisor and the deploy
  scripts read only statuses and numbers and are unaffected.

  On an install with no dashboard password nothing can sign in, so the
  dashboard header and both monitor pages show a note where that text used to
  be, from localhost too. Set a dashboard password (Configuration, Provider
  Keys) and sign in to see it again.
