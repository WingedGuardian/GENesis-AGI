"""Bake-off engines: nx_incremental (control), ladybug, falkor.

The control runs in-process (prod venv, which has networkx 3.6.1 + genesis). The
two contenders run in a throwaway venv subprocess (S2) — their modules are absent
from the prod venv, so ``available()`` reports False here and the harness skips
them until the S2 subprocess bridge lands.
"""
