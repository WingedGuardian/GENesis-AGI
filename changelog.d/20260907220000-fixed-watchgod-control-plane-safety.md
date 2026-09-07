- **The temp watchgod no longer damages the things it exists to protect, and it
  now reports a broken Claude Code control plane instead of leaving it silent.**
  Its weekly cleanup was aimed one directory level too shallow and judged
  staleness by directory timestamp, so a project folder whose sessions were all
  still active could be deleted whole — on one install that folder held 54 live
  session workspaces. It now reaps individual session folders, and only when
  nothing inside has been touched for seven days; if it cannot work out what
  "seven days ago" means, it deletes nothing at all. The ORANGE tier no longer
  kills idle Claude Code sessions: sessions are not what fills the temp
  directory, so the kill freed almost nothing while destroying a session's entire
  context. The /tmp housekeeping now leaves Claude Code's socket directories
  alone — an empty folder inside a live socket tree is a rendezvous point the
  daemon fills in later, not leftover junk.

  And a new check reports Claude Code sessions whose messaging socket has been
  deleted underneath them. They keep listening, so they look healthy while no
  other session can reach them, and nothing re-binds after startup — the only
  cure is restarting them. The count appears in the watchgod state file and pages
  once when it rises, staying quiet across restarts until it rises again. The
  check never deletes a socket, and says "unknown" rather than "fine" when it
  cannot see.
