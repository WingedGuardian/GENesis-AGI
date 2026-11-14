- **Emails to a marketing prospect wait for your approval unless you set the
  marketing lever to `live`.** The email gate now holds a send to a curated
  prospect whenever `marketing_outreach` is `off` or `observe`, whatever class
  the send has. Before, only the first cold email was held; a follow-up on an
  unanswered thread could go out on its own once cold sends had been granted.
  Two cases are held in every mode, `live` included: a reply to a prospect who
  wrote back, and any send to a prospect who opted out (approving that one
  still does not deliver it). Rejecting one of these held sends no longer
  costs the grant for other emails. Emails to anyone else are unchanged.
