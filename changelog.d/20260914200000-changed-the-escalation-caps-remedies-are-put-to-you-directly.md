- **When the review escalation cap blocks, its remedies are now put to you
  directly instead of being relayed.** Previously the gate printed its four
  options and the session retold them to you in its own words — which is how a
  real block once reached a user with the first option missing, a fourth
  invented, and "ship as-is" added, the one outcome that gate exists to prevent.
  Now, while the cap is the tier your next commit would hit, the gate's own
  question is appended to the next question the session asks you through the
  question tool, carrying the gate's own wording — the option text is a verbatim
  excerpt of the gate's message — in the gate's own order, with "hand it back"
  first. The session does not author those options, so it cannot drop, reword or
  pad them. Nothing else changes: the acknowledgement comments mean exactly what
  they meant before, and no gate reads your answer back. Turn the substitution
  off with `GENESIS_GATE_MENU_DISABLED=1` — any value except `0`, `false`, `no`
  and `off`, which name the switch's own position and so mean "leave it on" —
  or by creating `~/.genesis/config/gate_menu_disabled`. With it off, the gate
  blocks and the session asks exactly as it did before.

- **What the menu does NOT cover, stated so the claim is not wider than the
  thing.** It appears only for the escalation cap: the round-2 mode-switch tier
  and the final-round terminal still rely on the session relaying their options,
  and the terminal is excluded deliberately — its options are different, so
  showing the cap's menu there would be the very substitution error this exists
  to prevent. It only reaches you through the question tool, so a session that
  relays the block as ordinary prose is not covered at all, and a session that
  happens to ask four questions at once will not get the menu appended (the tool
  caps a call at four, and a rejected call would cost you the session's own
  questions too). It stops being offered as soon as the gate ACCEPTS your
  acknowledgement — which is not the same as a commit landing: the gate clears
  the streak the moment it recognises the acknowledgement, deliberately even if
  a later rule then blocks that same commit — except after "hand it back",
  where the gate deliberately
  forbids that acknowledgement, so the menu keeps being offered on that branch
  until you leave it or use the off switch. If a session is sitting in one
  worktree and committing into another, the menu will not appear. And a session
  in a long-lived worktree created before this change keeps that worktree's own
  frozen hook configuration, so the commit gate there still blocks — its entry
  is redirected to the current script — while the menu, which needs a new entry
  that worktree does not have, stays silent until the worktree catches up with
  the main branch.
