- **Four things that only broke on a genuinely fresh machine are fixed, found by
  the new fresh-install check on its very first run.** A clean install of Qdrant
  reported a setup warning anyway and printed "Genesis REQUIRES Qdrant" directly
  under the line saying it had just installed it. The `genesis` command and the
  auto-cd on login both assumed the repo lives at one specific path, so on any
  other clone location the command was installed already broken. One service unit
  shipped with a placeholder where a directory should be, which systemd rejects.
  And a server that failed to start printed a warning and carried on as though
  the install had succeeded. None of these are visible on a machine that already
  has Genesis running, which is why twelve weeks of installer changes went by
  without anyone noticing them.
- **A failed setup now says what went wrong.** The installer tracked warnings as
  a single yes/no flag set from eight different places, several of which print no
  warning text at all — so "setup completed with warnings" meant scrolling back
  through hundreds of lines to guess which one fired. Each warning now records its
  own reason, and both the summary and the strict-mode failure list them.
