- **The MCP health log can be moved off the shared home directory.** Importing
  the health MCP's browser module opens `~/tmp/mcp_health.log` and routes every
  `genesis.*` log line of that process into it, so a test run that merely
  imported the module wrote into the log the live MCP servers share.
  `GENESIS_MCP_LOG_DIR` now names that directory (and the Turnstile debug log's);
  unset keeps `~/tmp`, and a directory that does not exist attaches no log file.
