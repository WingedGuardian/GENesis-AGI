---
title: Close the remaining secrets-gate and provider-rule review findings
---

- Provider call patterns now match an explicit port (`api.openai.com:443`) and OpenAI `/v1/embeddings` (CodeRabbit Majors). `rg x <(curl <endpoint>)` no longer slips the read-only exemption — input process substitution is in `_CHAINS`. A quoted bare `secrets.env` that RESOLVES in cwd no longer gates as if it were an operand (`allow_bare` now honoured by the stat fallback), and globs deeper than two wildcard segments gate instead of walking past the hook budget. `behavioral_linter` and `secrets_env_access_guard` now fail CLOSED when a shared sibling module cannot be imported (was exit 1 = non-blocking allow), and a failed critical-observation write is logged to stderr instead of swallowed silently.
