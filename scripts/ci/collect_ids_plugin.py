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

import ast
import codecs
import copy
import hashlib
import inspect
import json
import os
import tokenize
from pathlib import Path

_errors: list[str] = []
_skipped: list[str] = []


def _definitions(path):
    """Index declarations, never discover tests. Unreadable source has no keys."""
    try:
        with tokenize.open(path) as source:
            if codecs.lookup(source.encoding).name not in {"utf-8", "utf-8-sig"}:
                return {}
            module = ast.parse(source.read())
    except (OSError, UnicodeError, SyntaxError, ValueError, RecursionError):
        return {}
    definitions = {}
    class_counts = {}

    def visit(node, classes):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            headers = []
            for cls in classes:
                header = copy.copy(cls)
                header.body = []
                headers.append(header)
            canonical = ast.dump(ast.Module(body=[*headers, node], type_ignores=[]))
            digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            start = min(n.lineno for n in [node, *node.decorator_list])
            identity = tuple(cls.name for cls in classes) + (node.name,)
            definitions.setdefault(identity, []).append((start, node.end_lineno, digest))
            return
        if isinstance(node, ast.ClassDef):
            identity = tuple(cls.name for cls in classes) + (node.name,)
            class_counts[identity] = class_counts.get(identity, 0) + 1
            classes = (*classes, node)
        for child in ast.iter_child_nodes(node):
            visit(child, classes)

    try:
        visit(module, ())
    except (RecursionError, UnicodeError):
        return {}
    return {
        identity: entries
        for identity, entries in definitions.items()
        if not any(
            class_counts.get(identity[:depth], 0) > 1 for depth in range(1, len(identity))
        )
    }


def _move_key(item, root, path, cache):
    """Only ordinary Python function items with an unambiguous source binding."""
    if type(item).__module__ != "_pytest.python" or type(item).__name__ != "Function":
        return None
    try:
        source = Path(item.path).resolve()
        source.relative_to(root)
        location, line, _ = item.location
        if (root / location).resolve() != source:
            return None
        obj = item.obj
        if inspect.ismethod(obj):
            obj = obj.__func__
        if not inspect.isfunction(obj) or Path(obj.__code__.co_filename).resolve() != source:
            return None
        identity = tuple(obj.__qualname__.split("."))
        if item.originalname != identity[-1]:
            return None
        prefix = path + "::"
        if not item.nodeid.startswith(prefix):
            return None
        suffix = item.nodeid[len(prefix) :]
        qualified = "::".join(identity)
        if suffix != qualified and not (suffix.startswith(qualified + "[") and suffix.endswith("]")):
            return None
        if source not in cache:
            cache[source] = _definitions(source)
        candidates = cache[source].get(identity, [])
        if len(candidates) != 1:
            return None
        start, end, digest = candidates[0]
        if not start <= line + 1 <= end or obj.__code__.co_firstlineno != start:
            return None
        return [suffix, digest]
    except (AttributeError, OSError, TypeError, ValueError, RecursionError):
        return None


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
    cache = {}
    move_keys = {}
    for item in session.items:
        key = _move_key(item, Path(root).resolve(), paths[item.nodeid], cache)
        if key is not None:
            move_keys[item.nodeid] = key
    out = os.environ["COLLECTED_FLOOR_OUT"]
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(
            {
                "ids": [item.nodeid for item in session.items],
                "paths": paths,
                "move_keys": move_keys,
                "errors": sorted(set(_errors)),
                "skipped": sorted(set(_skipped)),
            },
            fh,
        )
