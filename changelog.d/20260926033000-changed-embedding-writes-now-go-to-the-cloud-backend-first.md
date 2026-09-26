- **Embedding writes now go to the cloud backend first.** Storage previously led
  with the local Ollama backend, on the reasoning that a background write has no
  deadline so the slower local path costs nothing. That left out the CPU: local
  embedding is inference, and on a host without a GPU every memory write burned
  cores the rest of the system was contending for. Measured through the real
  chain, 20 calls each on representative memory-length texts: local p50 2395.8ms
  / p95 2952.9ms, cloud p50 207.8ms / p95 399.0ms — **11.5x at p50**. Ollama
  stays as the second rung, so a cloud outage degrades to local writes rather
  than losing them, and recall was already cloud-first. The change is to the
  chain builder's DEFAULT rather than one call site, because fifteen components
  construct an embedding provider without naming a chain — six in the package
  (execution traces, stale-embedding repair, procedural embedding and its
  promoter, the procedural MCP tool, the session-awareness worker) and nine
  maintenance scripts, the busiest being the MCP server that backs memory,
  reference and knowledge writes for every session. All fifteen are write paths,
  so flipping only the runtime wiring would have left them on local inference.
  Storage stays on the ordinary rate tier rather than the paid priority tier
  recall uses. Asking for local-first explicitly still works, and Ollama remains
  the fallback rung.

  **What this changes about where your data goes.** Memory and knowledge content
  is now sent to the cloud embedding provider at WRITE time, not only at recall.
  Retrieved bodies already left the host on the read path — the post-retrieval
  reranker scores `(query, document)` pairs remotely — so this is a change in
  when and how often, and it adds a second vendor to the set that sees stored
  text. Anything that must not leave the machine should not be stored in a body
  that retrieval can surface; that is true before this change and remains true
  after it.
