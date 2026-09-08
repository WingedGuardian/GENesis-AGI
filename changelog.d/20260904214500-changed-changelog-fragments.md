- **Changelog entries are now written one file at a time, so two branches
  never collide over them.** Add a file under `changelog.d/` instead of editing
  `CHANGELOG.md`; at release time they are folded into the `[Unreleased]`
  section and removed. Two branches writing two different filenames have
  nothing to merge, which is the entire point — a shared changelog is a file
  everyone edits at the same position, and git reports that as a conflict
  somebody has to resolve by hand.

  Measured against this repository's own queue beforehand: of 49 open pull
  requests, 21 could not merge, and 18 of those 21 conflicted on `CHANGELOG.md`
  and nothing else — every other file in them merged cleanly. A smarter merge
  setting cannot fix it, because GitHub ignores a repository's `.gitattributes`
  in its server-side merge; that was measured against GitHub's own merge engine
  rather than assumed.

  Nothing in the directory is skipped quietly. A misspelled name, a timestamp
  that is fourteen digits but not a real date, an unknown category, an empty
  file, or a nested folder is reported rather than ignored, because a fragment
  that is silently passed over is a changelog entry that never ships and that
  nobody notices — the release section still looks complete.
