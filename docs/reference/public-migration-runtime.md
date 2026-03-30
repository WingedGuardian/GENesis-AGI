# Public Migration Runtime Changes

This document records the runtime-facing changes made to support Genesis on a
fresh environment without assuming one developer machine.

## New Environment Contract

Genesis now resolves its machine-specific runtime values through env-backed
helpers in `src/genesis/env.py`.

Supported variables:

- `GENESIS_REPO_ROOT`
- `AZ_ROOT`
- `VENV_PATH`
- `SECRETS_PATH`
- `GENESIS_DB_PATH`
- `QDRANT_URL`
- `OLLAMA_URL`
- `LM_STUDIO_URL`
- `LM_STUDIO_HEALTH_URL`
- `GENESIS_ENABLE_OLLAMA`

Defaults remain compatible with the current private setup:

- Agent Zero defaults to `~/agent-zero`
- Genesis repo defaults to the current checkout root
- Qdrant defaults to `http://localhost:6333`
- Ollama defaults to `http://localhost:11434`

## Runtime Behavior Changes

- Secrets loading now uses the configured `SECRETS_PATH` instead of a hardcoded
  `~/agent-zero/usr/secrets.env`.
- Bridge boot uses the configured secrets path.
- Watchdog defaults to the configured secrets path.
- Memory/Qdrant init uses `QDRANT_URL`.
- Health probes and dashboard vitals use `QDRANT_URL` and `OLLAMA_URL`.
- Routing config is now env-aware and expands `${VAR}` and `${VAR:-default}`
  placeholders when loading `config/model_routing.yaml`.
- Provider init honors `GENESIS_ENABLE_OLLAMA=false` and skips Ollama provider
  registration when local inference is intentionally disabled.

## Service Behavior Changes

`config/genesis-bridge.service` and `config/genesis-watchdog.service` no longer
hardcode `${HOME}/...`. They now use:

- `%h/genesis` as the default repo location
- `%h/genesis/.venv` as the default venv
- shell-based startup so `GENESIS_REPO_ROOT` and `VENV_PATH` can be overridden

## Migration Utilities

- `scripts/capture_recovery_state.sh`
  Captures Genesis + Agent Zero git state, diffs, untracked files list, and
  version info before migration work.
- `scripts/check_portability.sh`
  Fails if runtime/config/script files still contain known machine-specific
  host/path assumptions.

## No-Ollama Support

Genesis still supports local Ollama, but it is now easier to run without it:

- set `GENESIS_ENABLE_OLLAMA=false` to disable local Ollama provider
  registration
- keep cloud reasoning providers configured for normal operation
- memory continues to degrade gracefully to FTS-only mode if embeddings are not
  available
