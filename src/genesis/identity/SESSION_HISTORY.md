## Session History (Reference Material)

You have access to Genesis's full conversation history. Session transcripts are
stored as JSONL files at:

    ~/.claude/projects/{project-id}/*.jsonl

where project-id is the repo path with `/` replaced by `-` (use `cc_project_dir()` from `genesis.env` or check `~/.claude/projects/` for the directory matching your repo)

Each file is one complete session. Each line is a JSON object with fields:
- `type`: "user", "assistant", "system", or "progress"
- `data`: the message content (structure varies by type)
- `timestamp`: ISO 8601
- `sessionId`: UUID linking to the cc_sessions table

Use these when historical context would inform your current task — prior
discussions about a technology, past architectural decisions, what the user
has said about a topic before, or how similar problems were solved previously.

Search with Grep (pattern across files) or Read (specific session). Do not
proactively index or summarize these — consult them on demand when relevant.
