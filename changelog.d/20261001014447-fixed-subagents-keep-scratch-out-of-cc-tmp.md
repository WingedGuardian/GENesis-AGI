- **Genesis's review and research subagents keep their scratch files out of
  Claude Code's shared temp.** Each agent definition now tells the agent to
  create its files in one directory of its own under `~/tmp`, pass that
  directory explicitly, cap reproductions that create many or large files,
  and clean up. A subagent writing to the default temp location could fill
  the space every session shares and break them all.
