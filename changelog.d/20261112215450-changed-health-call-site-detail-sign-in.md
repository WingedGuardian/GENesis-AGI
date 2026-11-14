- **Call-site response detail on the health endpoint now needs dashboard
  sign-in.** In `GET /api/genesis/health`, a call site's `last_response` goes
  to a signed-in dashboard session or the internal API token. Other callers get
  the rest of the site (status, provider, run time, token count) with
  `last_response_hidden: true` where a response exists. The Agent Zero
  adapter's copy of the route applies the same rule.

  On an install with no dashboard password nothing can sign in, so the neural
  monitor's **Last Response** box shows a note in place of the text, from
  localhost too. Set a dashboard password (Configuration, Provider Keys) and
  sign in to see it again. The health MCP tools read the snapshot in process
  and are unchanged.
