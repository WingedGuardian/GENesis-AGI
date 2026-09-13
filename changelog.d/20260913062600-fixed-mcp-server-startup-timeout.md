- **Claude Code sessions are far less likely to silently lose an MCP server that
  was slow to start.** A session starts eight MCP servers inside about eleven
  seconds, and
  Claude Code gives each one 30 seconds to connect. The heaviest Genesis server
  needs roughly 10 seconds on an idle machine — mostly import time before it runs
  any of its own code — so on a loaded box it can exceed the limit, and when it
  does the session simply runs without that server's tools for its entire life.
  Nothing announces this. On the install this was found on, a session had been
  running without all 34 memory tools and it was noticed only by reaching for one.

  The connect timeout is now raised to 120 seconds for every install. That is
  roughly eleven times the typical connect, so a server has to be genuinely stuck
  rather than merely slow before it is dropped. The cost is only paid when
  something really is broken: a hung server delays session start for longer before
  Claude Code gives up on it.

  This covers Genesis's own background sessions too, and that half matters more.
  A dispatched session runs outside the repository, so it never reads the
  project's settings and would otherwise have kept the old limit — and it is
  exactly the case where nobody is present to notice that a session spent an hour
  working without the memory it thought it had.

  This reduces how often the problem happens; it does not make it visible. Being
  told when a server fails to connect is tracked separately.
