"""Actual pipe identities prove the original owner alone retains retirement."""
import fcntl
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_scripts.test_code_intel_execution import evidence as evidence
from tests.test_scripts.test_code_intel_owner_loop import prepared
from tests.test_scripts.test_index_marker_control import base_queue as base_queue
from tests.test_scripts.test_index_marker_control import queue as queue


def matching_descriptors(pid, identity):
    result = []
    for path in (Path('/proc') / str(pid) / 'fd').iterdir():
        try:
            actual = path.stat()
        except FileNotFoundError:
            continue
        if (actual.st_dev, actual.st_ino) == identity:
            fields = dict(row.split(':', 1) for row in
                          (path.parent.parent / 'fdinfo' / path.name).read_text().splitlines())
            result.append((int(path.name), int(fields['flags'].strip(), 8) & os.O_ACCMODE))
    return sorted(result)


@pytest.mark.parametrize('failure', [None, 'pipe', 'spawn', 'birth', 'after_spawn'])
def test_owner_alone_holds_writer_and_every_exit_releases_it(queue, tmp_path, evidence, monkeypatch, failure):
    owner, snapshot, marker, claimed, namespace = prepared(queue, tmp_path, evidence, monkeypatch)
    original_os, original_subprocess = namespace['os'], namespace['subprocess']
    original_owned = namespace['OwnedChild']
    children, cancellation, observed = [], [], []

    def pipe():
        if children:  # Cancellation pipe is allocated after the foreground.
            if failure == 'pipe':
                raise OSError('injected cancellation allocation refusal')
            pair = original_os.pipe()
            cancellation.extend(pair)
            info = os.fstat(pair[0])
            observed.append((info.st_dev, info.st_ino))
            return pair
        return original_os.pipe()

    class ObservedPopen(original_subprocess.Popen):
        def __init__(self, command, **kwargs):
            if children:
                assert kwargs['stdin'] == cancellation[0] and kwargs['close_fds'] is True
                assert not kwargs.get('pass_fds')
                if failure == 'spawn':
                    raise OSError('injected monitor launch refusal')
            super().__init__(command, **kwargs)
            children.append(self)
            if len(children) == 2:
                identity = observed[0]
                assert matching_descriptors(children[0].pid, identity) == []
                assert matching_descriptors(self.pid, identity) == [(0, os.O_RDONLY)]
                assert matching_descriptors(os.getpid(), identity) == [
                    (cancellation[0], os.O_RDONLY), (cancellation[1], os.O_WRONLY)]
                assert not os.get_inheritable(cancellation[1])
                assert fcntl.fcntl(cancellation[1], fcntl.F_GETFD) & fcntl.FD_CLOEXEC

    def owned(child, proof):
        if len(children) == 2:
            with pytest.raises(OSError):
                os.fstat(cancellation[0])  # Closed before wrapper validation can fail.
            assert matching_descriptors(os.getpid(), observed[0]) == [(cancellation[1], os.O_WRONLY)]
            if failure == 'birth':
                raise ValueError('injected monitor identity refusal')
        return original_owned(child, proof)

    monkeypatch.setitem(namespace, 'os', SimpleNamespace(**{**vars(original_os), 'pipe': pipe}))
    monkeypatch.setitem(namespace, 'subprocess', SimpleNamespace(
        **{**vars(original_subprocess), 'Popen': ObservedPopen}))
    monkeypatch.setitem(namespace, 'OwnedChild', owned)
    if failure == 'after_spawn':
        def refuse():
            raise RuntimeError('injected post-launch observation refusal')
        monkeypatch.setattr(owner, 'early', refuse)
    result = owner.run(snapshot)
    if failure:
        assert result is None and not marker.exists()
        assert queue.execution_status(claimed['context'])['control_closure'] is None
    else:
        assert result['reason'] == 'startup_incomplete' and marker.read_text() == 'entered'
        assert result['monitor']['returncode'] == 0
    for identity in observed:
        assert matching_descriptors(os.getpid(), identity) == []
    for child in children:
        child.wait(timeout=3)  # Test cleanup only; never used as owner proof.
