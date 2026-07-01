"""Passive-listening attention engine (Track 1) — the deterministic L1 perk-up gate.

v1 runs OFFLINE, Genesis-side, in SHADOW mode over pulled read-only ambient
snapshots: it logs what it WOULD have perked up on (never speaks, never influences
output). The engine CORE — ``types``, ``clarity``, ``config``, ``triggers``,
``scorer``, ``engine`` — is a pure, genesis-dependency-free, event-``ts``-driven fold
so a batch replay over a snapshot is byte-identical to a future live edge run and the
core vendors to the edge voice repo unchanged (enforced by
``tests/test_attention/test_edge_portability.py``). Adapters — ``sources``,
``consumers``, ``snapshot``, ``runner`` — do all the I/O.

Design: ``~/.genesis/output/specs/attention-engine-design.md`` (§3 architecture,
§4 trigger taxonomy, §6 shadow mode, §11 the live-corpus eval).

NOTE: keep this package ``__init__`` free of adapter imports — importing the core
must never pull in ``genesis.db``/``genesis.routing``/etc. (see the edge-port test).
"""
