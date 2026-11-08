"""CI-only evidence capture; collection freezes the expected census before execution."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from genesis.eval.qualification.evidence import digest
from scripts.ci.codex_qualification_readback import SOURCE_PATHS, inventory

_cases = {}
_manifest_digest = None


def pytest_collection_finish(session):
    global _manifest_digest
    root = Path(session.config.rootpath)
    binary = Path(os.environ["GENESIS_CODEX_APP_SERVER_BIN"])
    manifest = {
        "schema": "codex-compatibility/v1", "head": os.environ["QUALIFICATION_HEAD"],
        "base": os.environ["QUALIFICATION_BASE"],
        "cases": [item.nodeid for item in session.items],
        "sources": inventory(root, SOURCE_PATHS),
        "binaries": {name: hashlib.sha256(binary.with_name(name).read_bytes()).hexdigest()
                     for name in ("codex", "codex-code-mode-host")},
    }
    path = Path(os.environ["QUALIFICATION_MANIFEST"])
    if session.config.option.collectonly:
        path.write_text(json.dumps(manifest, sort_keys=True))
    else:
        assert json.loads(path.read_text()) == manifest, "collection/source/native identity drift"
    _manifest_digest = digest(manifest)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    report = (yield).get_result()
    row = _cases.setdefault(item.nodeid, {"phases": []})
    row["phases"].append([report.when, report.outcome])
    if report.when == "teardown" and "tmp_path" in item.funcargs:
        root = item.funcargs["tmp_path"]
        candidates = [root / name for name in ("compat-evidence.json", "native-evidence.json")]
        found = [path for path in candidates if path.is_file()]
        if len(found) == 1:
            row["receipt"] = str(found[0].relative_to(Path(item.config.option.basetemp)))


def pytest_sessionfinish(session, exitstatus):
    if session.config.option.collectonly:
        return
    root = Path(session.config.option.basetemp)
    report = {"exitstatus": int(exitstatus), "cases": _cases, "manifest_digest": _manifest_digest,
              "artifacts": inventory(root, (".",))}
    target = Path(os.environ["QUALIFICATION_REPORT"])
    temporary = target.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, sort_keys=True))
    temporary.replace(target)
