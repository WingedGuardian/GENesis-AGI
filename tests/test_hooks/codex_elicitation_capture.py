"""Separate frozen census for MCP elicitation; never relax the inherited111."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from genesis.eval.qualification.evidence import digest, load_json
from scripts.ci.codex_elicitation_readback import CASES, INVENTORY, PREFIX
from scripts.ci.codex_qualification_readback import inventory

_cases = {}
_manifest_digest = None


def pytest_collection_finish(session):
    global _manifest_digest
    binary = Path(os.environ["GENESIS_CODEX_APP_SERVER_BIN"])
    assert binary.name == "codex" and Path(os.environ["GENESIS_CODEX_CLI_BIN"]).resolve() == binary.resolve()
    cases = [item.nodeid for item in session.items]
    assert len(cases) == len(CASES) and set(cases) == {f"{PREFIX}[{case}]" for case in CASES}
    manifest = {
        "schema": "codex-elicitation/v1", "head": os.environ["QUALIFICATION_HEAD"],
        "base": os.environ["QUALIFICATION_BASE"], "run_id": os.environ["ELICITATION_RUN_ID"],
        "attempt": os.environ["ELICITATION_ATTEMPT"], "cases": cases,
        "sources": inventory(Path(session.config.rootpath), INVENTORY),
        "binaries": {name: hashlib.sha256(binary.with_name(name).read_bytes()).hexdigest()
                     for name in ("codex", "codex-code-mode-host")},
    }
    assert all(manifest[name] for name in ("head", "base", "run_id", "attempt"))
    path = Path(os.environ["ELICITATION_MANIFEST"])
    if session.config.option.collectonly:
        path.write_text(json.dumps(manifest, sort_keys=True))
    else:
        assert load_json(path.read_bytes()) == manifest, "collection/source/native/run identity drift"
    _manifest_digest = digest(manifest)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    report = (yield).get_result()
    row = _cases.setdefault(item.nodeid, {"phases": []})
    row["phases"].append([report.when, report.outcome])
    if report.when == "teardown" and "tmp_path" in item.funcargs:
        path = item.funcargs["tmp_path"] / "elicitation-evidence.json"
        if path.is_file():
            row["receipt"] = str(path.relative_to(Path(item.config.option.basetemp)))


def pytest_sessionfinish(session, exitstatus):
    if session.config.option.collectonly:
        return
    report = {"exitstatus": int(exitstatus), "cases": _cases, "manifest_digest": _manifest_digest,
              "artifacts": inventory(Path(session.config.option.basetemp), (".",))}
    path = Path(os.environ["ELICITATION_REPORT"])
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, sort_keys=True))
    temporary.replace(path)
