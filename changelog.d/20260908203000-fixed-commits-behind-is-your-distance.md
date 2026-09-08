- **"N commits behind" now means how far behind YOU are.** The update notice
  counted the commits between the two release tags and labelled the result as
  your distance from the new version. On any install that pulls main between
  releases those are different numbers, and not by a little: the tag moves once a
  release while your deployment moves constantly. Measured on a live install, the
  dashboard read "v3.0b18 (668 commits behind)" on a tree that was twenty commits
  behind that tag. The figure was true about the release and false about the
  reader, which is worse than a wrong sum — it looked like a checked number, so
  nobody checked it.

  Both places that produced it are fixed, and the list of changes shown beneath
  it now covers the same range as the count, instead of ending "and 658 more"
  next to a figure the reader takes as their own. A third place in the codebase
  already measured this correctly and its field name said so; the two that were
  wrong shared a name that did not.
