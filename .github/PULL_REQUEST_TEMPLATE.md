## Summary

<!-- What does this PR do? Keep it brief. -->

## Related issue

<!-- If this PR resolves a tracked issue, link it with a CLOSING KEYWORD so the
     issue closes automatically when this merges to the default branch. A bare
     "#123" only cross-links — "Closes #123" (or Fixes/Resolves #123) closes it.
     Delete this section if there's no linked issue. -->

Closes #

## Changes

<!-- Bullet the key changes. -->

## Testing

<!-- How did you verify this works? -->

- [ ] `ruff check .` passes
- [ ] `pytest -v` passes
- [ ] `docs/architecture/CURRENT.md` updated (entry prose + `verified:` stamp) if subsystem capabilities changed

<!-- REQUIRED, and the merge gate enforces it: declare the POST-MERGE end-to-end
     verification this change needs. Replace the line below with one of:

       E2E: <one-line plan for the post-merge verification>
       E2E: none — <reason there is no runtime surface to verify>

     `none` is a legitimate answer for a docs/prose PR — what is not legitimate is
     leaving the decision unmade. Note it passes the gate but does NOT release the
     validator, which assumes every merged PR has an E2E and hunts for one anyway;
     the line is its first lead, not a boundary. (This guidance lives inside a
     comment on purpose: the gate strips comments before reading, so the template
     itself can never satisfy the gate.) -->

E2E:
