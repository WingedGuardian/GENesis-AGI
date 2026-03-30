---
name: shelve
description: >
  Shelve the current session — create a bookmark so you can find and resume
  it later with /unshelve.
---

# Shelve Session

Create a bookmark for the current session so you can return to it later.

## Arguments

The user may provide optional arguments after `/shelve`:
- `/shelve` — shelve with no tags
- `/shelve auth-refactor, critical` — shelve with tags "auth-refactor" and "critical"
- `/shelve auth-refactor | fixing the OAuth flow` — tags before `|`, context note after

## Steps

1. **Get the full session ID.** The Clock context line shows a truncated
   session ID (e.g., `Session: 802a856e`). To get the full UUID, list the
   session directories and match the prefix:

   ```bash
   ls ~/.genesis/sessions/ | grep "^<prefix>"
   ```

   If no match is found, tell the user the session context isn't available.

2. **Parse arguments.** Split on `|` if present:
   - Before `|`: comma-separated tags
   - After `|`: context note (trim whitespace)
   - If no `|`: treat everything as comma-separated tags

3. **Call `bookmark_shelve`** MCP tool with:
   - `session_id`: the full UUID from step 1
   - `tags`: comma-separated tag string (or empty)
   - `context`: the context note (or empty)

4. **Confirm.** Show the user the bookmark ID and the resume command:
   `claude --resume <full_session_id>`
