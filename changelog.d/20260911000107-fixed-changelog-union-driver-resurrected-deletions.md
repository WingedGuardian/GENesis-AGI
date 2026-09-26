- **A changelog entry removed at a release cut can no longer come back on its own.**
  `CHANGELOG.md` carried a `merge=union` attribute, which takes lines from both
  sides of a conflicting hunk and therefore cannot express a removal. Cutting a
  release moves every unreleased entry under a version heading, so any branch open
  across a release and merging the base afterwards could have the removed entries
  silently restored — no conflict, exit 0. Measured on a live branch: 287 removed
  lines were present again after an ordinary merge, in a branch that used
  `changelog.d/` fragments and never edited `CHANGELOG.md` deliberately.

  The attribute is removed, so such a collision now conflicts visibly and is
  resolved by taking the base's file and moving the entry into a fragment. The
  collision the attribute existed to smooth over is addressed structurally by
  `changelog.d/`, one fragment per change, which has no merge to get wrong.
