- **The decision registry validates through one strict pydantic model.**
  `DecisionSpec` and `Fallback` are now pydantic dataclasses with strict field
  types, so loading a file and constructing a spec in code run the same rules.
  - Identifiers have a grammar (ASCII snake_case), not just a type.
  - Text fields must be real strings, and `latency_budget_ms` a real integer.
  - Score levels must be distinct and ordered, and every threshold/band rule
    runs on construction.
  - `pydantic` is now a declared dependency. It was already installed through
    fastapi, litellm and mcp.
- **One YAML entry point.** Every parse failure is a `RegistryError`. The
  loader rejects duplicate keys, aliases, merge keys (`<<`), unhashable keys
  and non-canonical numbers (YAML 1.1 reads `0200` as 128, `1:30` as 90 and
  `0:0.5` as 0.5). An unreadable or mis-encoded file is a `RegistryError` too.
- **Overlays only override.** The shipped registry must validate on its own
  before any overlay is merged, so an overlay cannot mask a broken shipped file.
  - The `decisions.local.yaml` overlay is found where every other config
    overlay is: `~/.genesis/config/` first, then beside the shipped file.
  - An overlay may not add a decision or an option, and may not change a
    question's type, what it consumes, or its criteria.
  - An overlay may not set any field to null.
  - Unknown top-level keys and falsy non-mapping overlays are errors.
- **Gate verdicts name their fallback.** Legacy mode returns a `legacy`
  verdict that routes to `fallback.legacy` and needs no score. A
  calibrated-mode score abstains unless it carries a well-formed calibration
  version. `DecisionSpec.fallback_for()` resolves a verdict to its declared
  behaviour.
- **No site declares an outcome source it does not have.** Six sites named
  label sources that either did not exist or held other labels, so all are
  unset until a real persisted signal exists.
- **Three ego proposal decisions are registered:** reconcile, scope and
  realist, each described as its call site actually behaves. The registry
  header states that coverage is incomplete.
