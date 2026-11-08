"""Source admission stays bounded and preserves accepted immutable records."""

import contextlib
import errno
import json
import os
import select
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from genesis.transcript_analytics import store

_CHILD = r'''
import hashlib,json,os,sys
from pathlib import Path
from genesis.transcript_analytics import locks,store

root=Path(sys.argv[1]);when=sys.argv[2];initial=bool(int(sys.argv[3]))
projects=root/'projects';projects.mkdir();data=root/'data'
path=projects/('agent-source.jsonl' if when=='sidecar' else 'source.jsonl')
meta=path.with_suffix('.meta.json')
if when=='sidecar':meta.write_text('{}')
lock=root/'writer.lock'
def write(number):
    path.write_text(json.dumps({'type':'assistant','uuid':'event','sessionId':'session',
        'timestamp':'2026-01-01T00:00:00Z','message':{'id':'message','model':'fixture',
        'stop_reason':'end_turn','content':[],
        'usage':{'input_tokens':1,'output_tokens':number}}})+'\n')
write(1)
previous=None;before={}
if initial:
    assert store.ingest(projects,data,lock_path=lock)['rebuilt']==1
    previous=store.select_sources(data)[0][0]
    before={p.name:hashlib.sha256(p.read_bytes()).hexdigest()
            for p in previous.directory.iterdir() if p.is_file()}
    write(2)
    current=path.stat();os.utime(path,ns=(current.st_atime_ns,current.st_mtime_ns+1))
calls=[]
original_extract=store.extract_source
def observe_extract(*args,**kwargs):
    calls.append(True)
    return original_extract(*args,**kwargs)
store.extract_source=observe_extract
def substitute():
    target=meta if when=='sidecar' else path
    target.unlink();os.mkfifo(target)
if when=='discovery':
    original=store.discover
    def discovered(root):
        sources=original(root);substitute();return sources
    store.discover=discovered
else:
    original=store.build_source
    def before_open(*args,**kwargs):
        substitute();return original(*args,**kwargs)
    store.build_source=before_open
print('READY',flush=True)
result=store.ingest(projects,data,lock_path=lock)
selected,excluded=store.select_sources(data)
preserved=True
if initial:
    preserved=(len(selected)==1 and selected[0].directory==previous.directory
        and before=={p.name:hashlib.sha256(p.read_bytes()).hexdigest()
                     for p in previous.directory.iterdir() if p.is_file()})
with locks.writer(lock):
    pass
print(json.dumps({'failed':result['failed'],'rebuilt':result['rebuilt'],
    'extractor_calls':len(calls),'selected':len(selected),'excluded':len(excluded),
    'preserved':preserved,'writer_released':True}),flush=True)
'''


@pytest.mark.parametrize("when", ["discovery", "open", "sidecar"])
@pytest.mark.parametrize("initial", [False, True])
def test_fifo_substitution_is_rejected_before_extraction(tmp_path, when, initial):
    child = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(tmp_path), when, str(int(initial))],
        env=dict(os.environ, PYTHONPATH=str(Path(store.__file__).parents[2])),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        ready = b""
        deadline = time.monotonic() + 15
        while not ready.endswith(b"\n"):
            assert select.select(
                [child.stdout], [], [], max(0, deadline - time.monotonic())
            )[0], "synthetic ingest child never became ready"
            part = os.read(child.stdout.fileno(), 1)
            assert part, "synthetic ingest child exited before readiness"
            ready += part
        assert ready == b"READY\n"
        try:
            output, error = child.communicate(timeout=8)
        except subprocess.TimeoutExpired:
            pytest.fail("source opening blocked on a substituted FIFO while holding the writer")
        assert child.returncode == 0, error.decode()
        actual = json.loads(output)
        assert actual == {
            "failed": 1,
            "rebuilt": 0,
            "extractor_calls": 0,
            "selected": int(initial),
            "excluded": 0,
            "preserved": True,
            "writer_released": True,
        }
    finally:
        if child.poll() is None:
            child.terminate()
            child.communicate(timeout=5)


@pytest.mark.parametrize("failure", ["nonregular", "fstat", "extractor"])
def test_opened_source_descriptor_closes_on_rejection(tmp_path, monkeypatch, failure):
    path = tmp_path / "source.jsonl"
    path.write_text("{}\n")
    observed = path.stat()
    real_open, real_fstat = os.open, os.fstat
    opened, calls = [], []

    def capture(name, flags, *args, **kwargs):
        descriptor = real_open(name, flags, *args, **kwargs)
        if os.fspath(name) == os.fspath(path):
            opened.append(descriptor)
        return descriptor

    def inspect(descriptor):
        if descriptor in opened:
            if failure == "fstat":
                raise OSError(errno.EIO, "synthetic fstat failure")
            if failure == "nonregular":
                fields = list(real_fstat(descriptor))
                fields[0] = stat.S_IFIFO | 0o600
                return os.stat_result(fields)
        return real_fstat(descriptor)

    def extract(*args, **kwargs):
        calls.append(True)
        raise ValueError("synthetic extractor failure")

    monkeypatch.setattr(store.os, "open", capture)
    monkeypatch.setattr(store.os, "fstat", inspect)
    monkeypatch.setattr(store, "extract_source", extract)
    expected = OSError if failure == "fstat" else ValueError
    message = {
        "nonregular": "not a regular file",
        "fstat": "synthetic fstat failure",
        "extractor": "synthetic extractor failure",
    }[failure]
    try:
        with pytest.raises(expected, match=message):
            store.build_source(path, path.name, tmp_path / "data", observed)
        assert len(calls) == int(failure == "extractor")
        assert opened
        for descriptor in opened:
            with pytest.raises(OSError) as error:
                real_fstat(descriptor)
            assert error.value.errno == errno.EBADF
    finally:
        for descriptor in opened:
            with contextlib.suppress(OSError):
                os.close(descriptor)


@pytest.mark.parametrize("size", [0, 1048576, 1048577])
def test_sidecar_fingerprint_read_has_a_ceiling(tmp_path, size):
    source = tmp_path / "agent-source.jsonl"
    source.write_text("{}\n")
    source.with_suffix(".meta.json").write_bytes(b"x" * size)
    if size > 1048576:
        with pytest.raises(ValueError, match="metadata.*large"):
            store.source_fingerprint(source)
    else:
        assert store.source_fingerprint(source)["meta"]


@pytest.mark.parametrize("saved", [None, False, 17, [], {}, "relative", "/tmp/../other"])
def test_malformed_root_is_not_an_adoptable_missing_binding(tmp_path, saved):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    data.mkdir()
    assert store._projects_root(data, projects) == str(projects)
    (data / "projects-root.json").write_text(json.dumps({"projects_root": saved}))
    with pytest.raises(ValueError, match="projects.root"):
        store._projects_root(data, projects, adopt=True)


def _source(path):
    path.write_text(json.dumps({"type": "assistant", "uuid": "event", "sessionId": "s",
        "timestamp": "2020-01-01T00:00:00Z", "message": {"content": []}}) + "\n")


def test_ingest_keeps_the_root_captured_before_alias_repoint(tmp_path, monkeypatch):
    original, other, alias = tmp_path / "original", tmp_path / "other", tmp_path / "alias"
    original.mkdir()
    other.mkdir()
    _source(original / "source.jsonl")
    alias.symlink_to(original, target_is_directory=True)
    discover = store.discover
    def repoint(root):
        found = discover(root)
        alias.unlink()
        alias.symlink_to(other, target_is_directory=True)
        return found
    monkeypatch.setattr(store, "discover", repoint)
    data = tmp_path / "data"
    result = store.ingest(alias, data)
    assert result["rebuilt"] == 1 and result["projects_root"] == str(original)
    assert json.loads((data / "projects-root.json").read_text())["projects_root"] == str(original)
    with pytest.raises(ValueError, match="differs"):
        store.prune(data, before="2026-01-01", projects=other)
    assert store.prune(data, before="2026-01-01", projects=original) == 0


@pytest.mark.parametrize("number,stage", [
    (errno.EIO, "stat"), (errno.EACCES, "stat"), (errno.ELOOP, "stat"),
    (errno.ENOENT, "stat"), (errno.ENOENT, "extract"),
])
def test_source_stat_failures_preserve_later_work_and_inventory(tmp_path, monkeypatch, number, stage):
    projects, data = tmp_path / "projects", tmp_path / "data"
    projects.mkdir()
    bad, good = projects / "bad.jsonl", projects / "good.jsonl"
    _source(bad)
    _source(good)
    monkeypatch.setattr(store, "discover", lambda _: [(bad, bad.name), (good, good.name)])
    real_stat, build = os.stat, store.build_source
    def inspect(path, *args, **kwargs):
        if path == bad and stage == "stat":
            raise OSError(number, "synthetic source stat failure")
        return real_stat(path, *args, **kwargs)
    def extract(path, *args, **kwargs):
        if path == bad and stage == "extract":
            raise FileNotFoundError("synthetic source vanished")
        return build(path, *args, **kwargs)
    monkeypatch.setattr(store.os, "stat", inspect)
    monkeypatch.setattr(store, "build_source", extract)
    result = store.ingest(projects, data)
    assert result["rebuilt"] == 1 and not result["complete"]
    assert result["vanished"] == int(number == errno.ENOENT)
    assert result["failed"] == int(number != errno.ENOENT)
    assert result["sources"] == (1 if number == errno.ENOENT else 2)
    assert len(store.select_sources(data)[0]) == 1
    assert json.loads((data / "inventory.json").read_text()) == result
