"""pytest plugin: write the collected node ids of one tree to a JSON file.

Loaded by the CI ``collected-floor`` job with ``-p collect_ids_plugin`` during
``pytest --collect-only``, once on the PR tree and once on its base, and read by
``scripts/ci/collected_floor.py``.

WHY A PLUGIN AND NOT ``-q`` OUTPUT. Measured on pytest 9.0.3: parametrize ids
contain ``::`` (IPv6 literals such as ``fd00::1``), so splitting printed lines is
unsound, and a module skipped at import (``pytest.importorskip``) prints nothing
at all under ``-q``. The plugin records ``session.items`` exactly, plus the
collect reports that failed (collection errors) and were skipped (whole modules
skipped at import), so nothing depends on how a pytest version prints.

The file is written only from ``pytest_collection_finish``. A run that dies
before collection completes leaves no file, which the comparison refuses rather
than reading as an empty tree.

Output path: ``$COLLECTED_FLOOR_OUT``. Stdlib only.
"""

from __future__ import annotations

import json
import os

_errors: list[str] = []
_skipped: list[str] = []


def pytest_collectreport(report):
    if report.failed:
        _errors.append(report.nodeid)
    elif report.skipped:
        _skipped.append(report.nodeid)


def pytest_collection_finish(session):
    out = os.environ["COLLECTED_FLOOR_OUT"]
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "ids": [item.nodeid for item in session.items],
                "errors": sorted(set(_errors)),
                "skipped": sorted(set(_skipped)),
            },
            fh,
        )
