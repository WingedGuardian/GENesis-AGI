- Fixed the daily `git fsck` monitor paging on noise (#2745). Its alert now
  shows the lines that actually failed instead of thousands of harmless
  `dangling` objects, and a failure is re-checked two minutes later before it
  pages. A failure that passes the re-check, or whose re-check reports only
  objects that turn out to exist (a race with another git writer), is recorded
  as a `high` observation (morning report, dashboard), not paged. The scan also no longer
  holds up the awareness tick while it runs.
