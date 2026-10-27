- Same-owner peer replies now redact credential-shaped text in place
  (`[redacted:<pattern>]`) instead of withholding the whole reply, and private
  addresses, home paths and localhost ports pass through. A reply that is still
  unsafe after redaction (for example an escaped credential) is withheld as
  before. The task view lists what was redacted in `replyRedacted`.
