- **Inbox items replaced by a newer edit are no longer reported as failed.**
  When a file changed while its evaluation was parked on an approval, the old
  row was marked `failed`, so superseded snapshots made up nearly all inbox
  failures and buried the real ones. Those rows now get a new `superseded`
  status, keep their reason in `error_message`, and do not consume a retry. A
  schema migration adds the status and reclassifies existing rows whose reason
  is a supersession (`superseded by newer modification`, the older
  `superseded by new inbox scan`, and `content changed`); other invalidation
  reasons (approval cancelled/expired, source file deleted, content removed
  before retry) stay `failed`. The user-ego inbox backlog count accounts for
  the new status, and the inbox overlay gains a superseded tab; each status tab
  now asks the server for that status instead of filtering the newest 50 rows,
  which could hide older rows of the chosen status. `scripts/inbox_check.py`
  now applies pending schema migrations before it runs. Detection behaviour is
  unchanged: superseded rows were already invisible to the known-file scan
  and the retry lane.
