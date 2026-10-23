- **The closing-session skill is about a sixth shorter.** It went from 2,494
  to 2,080 lines. Text repeated between its original half and the half moved
  in from `genesis-development` now has one copy. Gate internals a closing
  session reads only on demand moved to references:
  - `genesis-development/references/gate-fix-lane.md` (new), the protocol for
    PRs that touch the enforcement-hook surface;
  - `release-review.md` (new);
  - `merge-gate-findings.md`.

  Six places where the two copies disagreed now follow the newer owner ruling,
  or the gate code. The Load Gate states that there is no "repair" session type.
