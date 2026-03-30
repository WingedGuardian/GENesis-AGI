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
3. Test manually: `.claude/hooks/genesis-hook my_hook.py`

## Inline Bash Hooks

Some safety guards (pip-editable blocker, YouTube URL blocker) are inline bash
in settings.json. These don't use the launcher — they're self-contained bash
one-liners with no Python dependencies.
