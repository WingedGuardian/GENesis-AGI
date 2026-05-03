# Three Months Later, It Remembers

Most AI tools lose context between sessions. Some let you paste in previous
conversation manually. Some have "memory" features that store what seems important
and surface it later. The problem with the latter isn't the storage. It's the
retrieval.

Context only helps when the right piece surfaces at the right moment. Everything
else is noise.

---

## The Scenario

Three months ago, you decided to keep Genesis's two Qdrant collections separate:
episodic memory for internal decisions and reflections, a separate knowledge base
for external reference material. The rationale was about lifecycle: episodic memory
decays and gets corrected over time, external knowledge doesn't. You made that call,
committed the code, and moved on to the next problem.

Today, you're looking into a retrieval behavior you didn't expect. You describe
it to Genesis. Before your question is fully processed, the relevant memory surfaces
automatically: the collection separation decision, the rationale, and a note you
left about the trade-off at the time.

You didn't search for it. Genesis didn't dump three months of architecture notes
into context. The right memory came up because the query matched what was stored,
and because that decision was important enough to still be activated.

---

## How It Works

Every Genesis session gets a baseline injection of ~300 tokens: active context,
recent decisions, and a wing index. This comes from pure DB queries, no network
dependency. If the question is "what are we working on," this layer answers it
without touching the retrieval system.

Beyond that, every prompt triggers a secondary retrieval pass: keyword search
against SQLite FTS5 (always available) combined with vector similarity search
against Qdrant (1024-dimensional embeddings, cosine distance). The results from
both are fused using Reciprocal Rank Fusion, a ranking method that combines
multiple ordered result lists without requiring a trained model. The formula:
for each memory in each ranked list, `score += 1 / (k + rank)`, with k=60. A
memory appearing in multiple lists accumulates score from each. The top results
inject automatically.

What determines whether an old decision stays surfaceable is activation scoring.
It's not just recency. Activation is a product of confidence, recency (exponential
decay with source-aware half-lives), access frequency, and graph connectivity. An
architectural decision that's been referenced multiple times and linked to other
memories scores higher than a casual observation from last week, even if the
observation is newer.

The half-lives are calibrated to information type. Session decisions decay over
60 days; reflections over 30. Memories that reference proper nouns get double
the half-life, because they tend to stay relevant longer. "Qdrant configuration"
decays slower than "tried a different approach."

---

## The Outcome

When you come back after a week away, Genesis has a working model of what you were
building. When something from months ago is relevant, it surfaces without you having
to ask. When you make a correction, that correction persists with metadata about
the original memory, so the store accumulates not just what happened but what was
learned.

A typical installation at several months old holds 10,000+ memories across
architectural decisions, configuration rationale, research findings, feedback from
corrections, and patterns observed across sessions. Retrieval stays useful not because
everything is injected but because what's injected is scored for relevance.

---

## Why This Matters

The failure mode for long-running AI integrations is context erosion. Every session
starts from scratch. The system's usefulness flatlines because it never builds a
model of what you're working on, how you approach problems, or what you've already
tried. That's tool behavior.

What separates a useful tool from a genuinely useful system is whether it compounds.
Each session builds on the last. Decisions made in month one still inform work in
month six. That's what persistent memory with working retrieval actually enables.

---

*For the implementation details behind this case study, see
[`docs/architecture/memory-deep-dive.md`](../architecture/memory-deep-dive.md).*
