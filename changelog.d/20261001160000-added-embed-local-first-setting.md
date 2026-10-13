- **Embedding backend order is now an install setting.** Set
  `memory.embed_local_first: true` in `~/.genesis/config/genesis.yaml` (or
  `GENESIS_EMBED_LOCAL_FIRST=true`) to put the local Ollama backend ahead of the
  cloud one. Unset, the order stays cloud-first with Ollama as the fallback.
  It applies to every chain built without an explicit order: runtime storage
  and recall, the memory MCP server, and default providers. It changes the
  order only, never which model writes. Takes effect on restart.
