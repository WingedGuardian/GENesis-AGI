---
name: unshelve
description: >
  Search for shelved sessions — find past bookmarked sessions by keyword
  or browse recent ones.
---

# Unshelve Session

Find and resume a previously shelved session.

## Arguments

- `/unshelve` — show the 5 most recent bookmarks
- `/unshelve auth` — search bookmarks matching "auth"
- `/unshelve plan approved` — search for plan sessions

## Steps

1. **Call `bookmark_unshelve`** MCP tool with:
   - `query`: the user's search text (or empty string for recent)
   - `limit`: 5 (default)

2. **Display results.** For each bookmark, show:
   - Topic and tags
   - When it was created
   - Whether it has a rich summary
   - The resume command: `claude --resume <session_id>`
