- **PR-watch notifications are credited to the code that produces them.** Five
  files described the `[PRs] …` session nudge as mirroring "the
  `upstream-pr-steward` campaign's own owner notifications". No such campaign
  exists. MEASURED: #792 added the `steward` DirectSession profile and referred
  to an intended "upstream-PR stewardship campaign" in a code comment, but the
  hyphenated slug appears **0 times** in its diff; #1182 coined that slug and
  wrote **all five** prose sites in a single commit. So this was not a claim
  inherited down a chain of commits — one change minted the name and every
  reference to it at once, and no campaign by that name exists. The rows
  `pr_watch` reads are written by `recon/account_activity.py` — a poller inside
  genesis-server whose topics are prefixed `GitHub steward: `, which is what the
  `%steward%` LIKE actually matches — and by the `github-activity-digest`
  campaign's digests. Prose only, no behaviour change; the corrected text names
  both real producers and records that the campaign does not exist, so the
  attribution is not re-derived from the old wording.
