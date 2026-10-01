---
name: gitnexus-cli
description: "Use when the user needs to run GitNexus CLI commands like analyze/index a repo, check status, clean the index, generate a wiki, or list indexed repos. Examples: \"Index this repo\", \"Reanalyze the codebase\", \"Generate a wiki\""
---

# GitNexus CLI Commands

Genesis uses the reviewed global GitNexus pin. Do not use `npx`: its floating
package resolution can write an index format the reviewed binary cannot read.

## Commands

### analyze — Build or refresh the index

```bash
scripts/lib/code_intel_index.sh "$PWD" gitnexus fast
```

Run from the project root. The managed entrypoint provides single-flight and
resource caps while GitNexus refreshes `.gitnexus/`.
It deliberately skips linked worktrees; use Serena for live worktree truth.

The managed entrypoint accepts Genesis's `fast`, `moderate`, or `full` mode as
its final argument. It deliberately does not forward arbitrary GitNexus flags.

**When to run:** First time in a project, after major code changes, or when `gitnexus://repo/{name}/context` reports the index is stale. In Claude Code, a PostToolUse hook detects staleness after `git commit` and `git merge` and notifies the agent to run `analyze` — the hook does not run analyze itself, to avoid blocking the agent for up to 120s and risking index corruption on timeout.

### status — Check index freshness

```bash
.claude/mcp/run-gitnexus status
```

Shows whether the current repo has a GitNexus index, when it was last updated, and symbol/relationship counts. Use this to check if re-indexing is needed.

### clean — Delete the index

```bash
.claude/mcp/run-gitnexus clean
```

Deletes the `.gitnexus/` directory and unregisters the repo from the global registry. Use before re-indexing if the index is corrupt or after removing GitNexus from a project.

| Flag      | Effect                                            |
| --------- | ------------------------------------------------- |
| `--force` | Skip confirmation prompt                          |
| `--all`   | Clean all indexed repos, not just the current one |

### wiki — Generate documentation from the graph

```bash
.claude/mcp/run-gitnexus wiki
```

Generates repository documentation from the knowledge graph using an LLM. Requires an API key (saved to `~/.gitnexus/config.json` on first use).

| Flag                | Effect                                    |
| ------------------- | ----------------------------------------- |
| `--force`           | Force full regeneration                   |
| `--model <model>`   | LLM model (default: MiniMax-M3)           |
| `--base-url <url>`  | LLM API base URL                          |
| `--api-key <key>`   | LLM API key                               |
| `--concurrency <n>` | Parallel LLM calls (default: 3)           |
| `--gist`            | Publish wiki as a public GitHub Gist      |

### list — Show all indexed repos

```bash
.claude/mcp/run-gitnexus list
```

Lists all repositories registered in `~/.gitnexus/registry.json`. The MCP `list_repos` tool provides the same information.

## After Indexing

1. **Read `gitnexus://repo/{name}/context`** to verify the index loaded
2. Use the other GitNexus skills (`exploring`, `debugging`, `impact-analysis`, `refactoring`) for your task

## Troubleshooting

- **"Not inside a git repository"**: Run from a directory inside a git repo
- **Index is stale after re-analyzing**: Restart Claude Code to reload the MCP server
- **Embeddings slow**: Omit `--embeddings` (it's off by default) or set `OPENAI_API_KEY` for faster API-based embedding
