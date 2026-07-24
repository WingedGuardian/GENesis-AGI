# Genesis Hook System

## Architecture

Hook scripts live in `scripts/` (the Genesis convention for all scripts).
The launcher `.claude/hooks/genesis-hook` provides portable invocation:

```
settings.json hook command
       |
       v
  ${CLAUDE_PROJECT_DIR}/.claude/hooks/genesis-hook <script_name>.py
       |
       v
  Resolves genesis root -> finds .venv/bin/python -> runs scripts/<script>.py
```

## How It Works

- `genesis-hook` is a bash script that self-locates the genesis root
- It finds the Python venv (with worktree fallback to the main repo)
- Hook scripts stay in `scripts/` — they're Genesis scripts, not CC-specific
- `$CLAUDE_PROJECT_DIR` is injected by Claude Code, making paths portable

## Adding a New Hook

1. Create the script in `scripts/my_hook.py`
2. Add it to `.claude/settings.json` under the appropriate event:
   ```json
   {
     "type": "command",
     "command": "${CLAUDE_PROJECT_DIR}/.claude/hooks/genesis-hook my_hook.py",
     "timeout": 2000
   }
   ```
3. Test manually: `echo '<payload>' | .claude/hooks/genesis-hook my_hook.py`
   (payload on **stdin** — see the input contract below).

## Hook Input Contract — read stdin, never the `CLAUDE_TOOL_INPUT` env var

Claude Code delivers each hook's payload as **JSON on stdin**, e.g.
`{"tool_name": "Bash", "tool_input": {"command": "..."}, "tool_response": {...},
"session_id": "..."}`. Tool arguments are nested under `tool_input`.

A hook must read its input through the shared helper, **never** from
`os.environ["CLAUDE_TOOL_INPUT"]` (or `CLAUDE_TOOL_USE_RESULT` / `CLAUDE_SESSION_ID`).
Those env vars are a dead legacy contract — current CC does not set them, so a
hook that reads them fails open silently (this is exactly how a dozen guards went
inert; see `docs/reference/cc-compatibility.md`).

```python
from hook_input import read_payload, field, tool_response, session_id  # scripts/hooks/

payload = read_payload()                 # full payload dict ({} on failure)
cmd = field(payload, "command")          # tool_input.command (also handles Write's file_path, etc.)
result = tool_response(payload)          # PostToolUse result
sid = session_id(payload)                # session id (for per-session sentinels)
```

Scripts in `scripts/` (not `scripts/hooks/`) reach the helper with a one-line
bootstrap: `sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "hooks"))`.

A settings.json command must NOT wrap the hook in `echo "$CLAUDE_TOOL_INPUT" | …`
— that clobbers CC's real stdin with an empty value. Invoke the hook directly and
let CC's stdin flow through.

`tests/test_scripts/test_hook_input_contract.py` enforces this: it feeds each
safety-critical guard a real payload and fails if any hook reads a dead env var.

## Inline Bash Hooks

Some safety guards (pip-editable blocker, YouTube URL blocker) are inline bash
in settings.json. These don't use the launcher — they're self-contained bash
one-liners with no Python dependencies. They read the payload from stdin too:
`IN=$(cat); CMD=$(printf %s "$IN" | jq -r .tool_input.command)`.
