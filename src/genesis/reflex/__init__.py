"""Reflex arc — autonomous self-bug detection & repair.

The afferent nerve for Genesis's own screaming bugs: ``task.failed`` events
are fingerprinted and deduplicated into ``reflex_signals``; later phases add
the diagnose/fix card lanes. Design spec:
``docs/superpowers/specs/2026-07-21-reflex-arc-design.md``.
"""
