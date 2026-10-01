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
  reference and knowledge writes for every session. Flipping only the runtime
  wiring would have left them on local inference. Storage stays on the ordinary
  rate tier rather than the paid priority tier recall uses; the standalone memory
  MCP server now builds a separate priority-tier provider for recall, as the full
  runtime already did, so interactive recall does not inherit the ordinary tier's
  queue. Asking for local-first explicitly still works, and Ollama remains the
  fallback rung.

  **Every embedding chain now uses exactly one model.** Backends are only
  interchangeable if they produce vectors in the same space; matching dimension
  is not enough. DeepInfra and the local Ollama model are the same Qwen3 0.6B
  model (measured cosine 0.9997-0.9999 on identical text), but the DashScope
  backend is a different model and is no longer mixed into a Qwen3 chain — before
  this, an install with DashScope and Ollama but no DeepInfra would have written
  DashScope vectors into its Qwen3 memory collection. On such an install recall
  now runs on the local model only, which is slower but no longer compares
  vectors from two different models. DashScope is still used when it is the
  only backend configured. A provider assembled by hand from
  backends in different spaces now refuses to start, and the embedding cache is
  keyed by vector space (a one-time cold cache after updating). Every embedding
  backend must now declare the vector space it produces; a custom backend that
  does not is refused rather than silently mixed in. The dashboard's embedding
  panel now reads the chains the server actually built and names the model that
  wrote the server process's most recent vector, so a fallback to the local
  model during a cloud outage is marked as a fallback, with the configured
  primary beside it. (Writes made by a session's own memory tools run in a
  separate process and are not reflected there.) The LongMemEval harness,
  which writes into brand-new throwaway collections, now keeps its cloud-first
  chain even when the local model is a different one.

  **What this changes about where your data goes.** Memory and knowledge content
  is now sent to the cloud embedding provider at WRITE time, not only at recall.
  Retrieved bodies already left the host on the read path — the post-retrieval
  reranker scores `(query, document)` pairs remotely — so this is a change in
  when and how often, and it adds a second vendor to the set that sees stored
  text. Anything that must not leave the machine should not be stored in a body
  that retrieval can surface; that is true before this change and remains true
  after it.
