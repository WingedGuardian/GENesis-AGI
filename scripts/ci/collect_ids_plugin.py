"""pytest plugin: write the collected node ids of one tree to a JSON file.

Loaded by the CI ``collected-floor`` job with ``-p collect_ids_plugin`` during
``pytest --collect-only``, once on the PR tree and once on its base, and read by
``scripts/ci/collected_floor.py``.

WHY A PLUGIN AND NOT ``-q`` OUTPUT. Measured on pytest 9.0.3: parametrize ids
contain ``::`` (IPv6 literals such as ``2001:db8::1``), so splitting printed lines is
unsound, and a module skipped at import (``pytest.importorskip``) prints nothing
at all under ``-q``. The plugin records ``session.items`` exactly, each item's
file path (so no id is ever split to find its file), plus the
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
    # Each item's real file, relative to the rootdir, so the comparison never
    # has to cut a node id apart: a file name may itself contain ``::``.
    # os.fspath keeps undecodable bytes as surrogates, the same convention
    # os.fsdecode applies to the changed-file list.
    root = str(session.config.rootpath)
    paths = {item.nodeid: os.path.relpath(os.fspath(item.path), root) for item in session.items}
    out = os.environ["COLLECTED_FLOOR_OUT"]
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "ids": [item.nodeid for item in session.items],
                "paths": paths,
                "errors": sorted(set(_errors)),
                "skipped": sorted(set(_skipped)),
            },
            fh,
        )
