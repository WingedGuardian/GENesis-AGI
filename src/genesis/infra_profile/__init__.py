"""Infrastructure body schema — programmatic self-knowledge of the machine.

Deterministic collectors → hashed fact sections (``profile.json``) → rendered
``INFRASTRUCTURE.md`` → LLM gotcha annotations pinned to fact hashes → drift
observations when a section's facts change.

Deliberately import-light: consumers import the submodule they need
(``service``, ``render``, ``store``, ``claude_md``) so entry points like
``python -m genesis.infra_profile --claude-md-block`` never drag in the
collector or runtime graphs.
"""
