- **Genesis now remembers which merged PRs still need their end-to-end check.**
  "Verify this after it merges" used to live in whoever merged it — measured, one
  of two owner-directed merges had its E2E forgotten until someone asked, and both
  found real problems once run. Every merged PR now gets a durable row, and a
  docs-only change closes its own row automatically with the reason recorded, so
  the list holds real work rather than noise. Documentation and prompt/skill edits
  are exempted by a fixed path rule, never by a model deciding case-by-case. See
  what is outstanding with
  `python scripts/repo_pulse_worker.py --verification-backlog`; turn the whole
  thing off with `verification_enabled: false` in `config/repo_pulse.yaml`.
  Recording starts from the merges the pulse worker sees after the upgrade; it
  does not reach back over history already behind the worker's cursor. Verified
  against a scratch copy of this repo's real merge history — 105 of 105 merges
  recorded, 21 auto-exempted as documentation — which is a check of the
  behaviour, not a description of rows your install will have.
