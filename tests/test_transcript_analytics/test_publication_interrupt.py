"""Real SIGKILL confirms bounded publication progress and fingerprint reuse."""

import json
import os
import subprocess
import sys
import time

import pytest

from genesis.transcript_analytics import catalog, store


@pytest.mark.parametrize("retained", [False, True])
def test_first_changed_source_commits_each_run_before_large_source(tmp_path, retained):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    record = (
        json.dumps(
            {
                "type": "assistant",
                "sessionId": "s",
                "uuid": "u",
                "message": {"id": "m", "content": [{"type": "text", "text": "safe"}]},
            }
        )
        + "\n"
    )
    lock = tmp_path / "writer.lock"
    if retained:
        (projects / "old.jsonl").write_text(record)
        store.ingest(projects, data, lock_path=lock)
    (projects / "a.jsonl").write_text(record)
    (projects / "b.jsonl").write_text(record)
    ready = tmp_path / "inflight"
    script = tmp_path / "child.py"
    script.write_text("""import sys,time
from pathlib import Path
from genesis.transcript_analytics import store
projects,data,lock,ready=map(Path,sys.argv[1:])
original=store.extract_source
discover=store.discover
store.discover=lambda root: sorted(discover(root),key=lambda item:item[1])
def extract(path,*args,**kwargs):
    if path.name=="b.jsonl":
        ready.touch()
        time.sleep(60)
    return original(path,*args,**kwargs)
store.extract_source=extract
store.ingest(projects,data,lock_path=lock)
""")
    child = subprocess.Popen(
        [sys.executable, str(script), str(projects), str(data), str(lock), str(ready)],
        env=dict(os.environ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 30
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert ready.exists(), (
            child.communicate(timeout=1)[1]
            if child.poll() is not None
            else "child did not reach inflight source"
        )
        child.kill()
        child.wait(timeout=5)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        child.stderr.close()
    selected = catalog.load(data)
    assert not selected.collection["complete"]
    assert store.srckey("a.jsonl") in selected.sources
    assert store.srckey("b.jsonl") not in selected.sources
    generation = selected.sources[store.srckey("a.jsonl")]
    result = store.ingest(projects, data, lock_path=lock)
    assert result["rebuilt"] == 1
    assert catalog.load(data).sources[store.srckey("a.jsonl")] == generation
    assert catalog.load(data).collection["complete"]
