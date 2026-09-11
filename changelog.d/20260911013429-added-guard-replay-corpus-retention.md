- The daily disk-hygiene run now ages out the guard replay corpus cache
  (`~/.genesis/output/guard-corpus.jsonl`) and any temp left by an interrupted
  rebuild after 45 days. The cache is regenerable and holds verbatim command
  lines from your own sessions, so it no longer lives forever by default.
