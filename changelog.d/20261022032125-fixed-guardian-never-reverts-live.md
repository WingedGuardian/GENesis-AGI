- **The Guardian never reverts code on a `live` checkout, and its diagnosis
  names the boot repair.** Where the container checkout is on `live` (the
  integration branch the deploy manifest builds), or could not be shown not to
  be, a proposed `REVERT_CODE` now becomes `RESTART_CONTAINER`, the ladder's
  next rung, before the action approval, so the approval names what will run.
  A container restart ends any process still holding the update or checkout
  lock and lets a boot repair run where one is installed. It does not fix a bad
  candidate's code: that candidate is removed with
  `scripts/deploy_candidates drop <branch>`, which the Guardian will run itself
  in a later change. `REVERT_CODE` also refuses on its own, because its
  `git stash` would put a half-written tree on the stash every session shares
  and its revert would commit onto a branch the deploy engine rebuilds. Every
  other branch reverts as before. The diagnosis now carries the boot repair's
  recent journal lines and a summary of its newest current `REPAIR.json`
  (result, counts, and up to ten of the paths that were not the update's),
  every value quoted, bounded and marked as untrusted container data; it adds
  nothing when there is none. The diagnosis prompt now states that everything
  collected from the container is data, never an instruction, and each field
  of a Guardian Telegram alert is cut to a bound, so a long alert is no longer
  refused by Telegram's 4,096-character limit. The Guardian reaches the host
  only through `scripts/update.sh`.
