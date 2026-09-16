- **The merge gate now reads review findings it previously could not see at all.**
  When a reviewer's finding points at a line outside the changes a pull request
  actually made, CodeRabbit cannot attach it to the diff and puts it in the review
  summary instead. The gate only ever read the attached findings, so those went
  unreported — a pull request could show a clean review status with real findings
  outstanding. Measured on the install this was built against: 27 findings across
  23 open pull requests were invisible this way, 15 of them rated Major, including
  one about silently losing a stored preference and one about writing unredacted
  URLs to a log.

  Those findings are now collected and shown, deduplicated across re-reviews and
  kept at the highest severity any review gave them. Only a Critical one holds up
  a merge: the rest are reported but do not block, because a finding delivered
  this way has no comment thread, and the usual way to accept a finding you
  disagree with is to reply in its thread. Blocking on something you cannot answer
  would leave no way forward.

  A review summary quotes code, so it can contain text that looks like the
  summary's own structure. Findings are therefore located by where they sit in the
  document rather than by matching that text wherever it appears, which stops a
  finding being hidden or filed against the wrong file by content in the pull
  request under review. If the summary says it carries more findings than were
  read, the merge is held rather than proceeding on a partial list.
