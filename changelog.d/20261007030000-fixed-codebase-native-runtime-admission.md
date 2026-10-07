Fixed managed Codebase query startup to use the installed venv interpreter,
verify physical CPU/task ceilings, admit the full query budget against current
reclaim-aware ancestor usage at startup, and bound readiness around native
appearance without prematurely consuming its RPC window during preflight.
