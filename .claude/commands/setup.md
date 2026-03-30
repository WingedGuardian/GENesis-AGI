Run the Genesis bootstrap script to set up or repair the Claude Code configuration
on this machine:

```bash
./scripts/bootstrap.sh
```

For just the CC config (without full bootstrap):
```bash
python scripts/setup_claude_config.py          # .mcp.json only
python scripts/setup_claude_config.py --global  # Also update ~/.claude/settings.json
```
