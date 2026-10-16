- Qualification now retains explicit invalid evidence for excessive or malformed
  response and billing JSON, redacts unsafe escaped diagnostics, and avoids
  repeated matching of malformed quoted text. Recovery remains GET-only and
  never resends a dispatched completion.
