- **The health endpoint no longer returns a call site's last model response to
  a caller without a credential.** `GET /api/genesis/health` answers every
  network, and each call site in it carried the text that site's last model
  call returned. That text now goes only to a signed-in dashboard session or
  the internal API token. Every other caller gets the rest of the site
  (status, provider, run time, token count) with `last_response_hidden: true`
  where a response exists. The Agent Zero adapter's own copy of the route now
  applies the same reduction, which also brings it the existing withholding of
  the deploy checkout's edited file names.

  On an install with no dashboard password nothing can sign in, so the neural
  monitor's **Last Response** box shows a note in place of the text, from
  localhost too. Set a dashboard password (Configuration, Provider Keys) and
  sign in to see it again. The health MCP tools read the snapshot in process
  and are unchanged.
