"""Bake-off engines: nx_incremental (control), ladybug, falkor.

The control runs in-process (prod venv, which has networkx 3.6.1 + genesis). The
two contenders run in a throwaway venv subprocess (S2). Ladybug's module is
absent from the prod venv, so its ``available()`` reports False here. Falkor's
is not: ``falkordblite`` is a Genesis dependency now (it ships the graph engine),
so its ``available()`` is True wherever that wheel is installed, while its
``load``/``run`` remain S2 stubs.
"""
