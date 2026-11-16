"""Startup observations share the existing private output, with atomic framing."""
import os
import time
from copy import deepcopy

import pytest

from tests.test_scripts.test_code_intel_startup_gate import _gate
from tests.test_scripts.test_code_intel_startup_gate import handshake as handshake
from tests.test_scripts.test_index_marker_legs import queue as queue


def record(handshake):
    queue, context, reserved, value, namespace = handshake
    scope = value['scope']
    return dict(reservation_sha256=queue._native.digest(reserved), scope_sha256=queue._native.digest(scope),
                facts=deepcopy(scope['facts']), observed_ns=time.monotonic_ns())


def test_atomic_observation_keeps_existing_output_open_for_later_samples(handshake):
    gate = _gate(handshake)
    scope = handshake[3]['scope']
    value = record(handshake)
    reader, writer = os.pipe()
    try:
        operation = getattr(gate, 'emit', None)
        assert callable(operation), 'fresh startup facts require bounded existing-output emission'
        operation(writer, scope, value)
        assert os.read(reader, 4096) == handshake[0]._native.encode(value) + b'\n'
        os.fstat(writer)
        assert not os.get_inheritable(writer)
        value['facts']['events']['high'] += 1
        value['observed_ns'] = time.monotonic_ns()
        operation(writer, scope, value)
        assert os.read(reader, 4096) == handshake[0]._native.encode(value) + b'\n'
    finally:
        os.close(reader)
        os.close(writer)


@pytest.mark.parametrize('fault', ['reservation', 'scope', 'leaf', 'extra', 'time', 'future'])
def test_invalid_startup_observation_never_becomes_partial_output(handshake, fault):
    gate = _gate(handshake)
    value = record(handshake)
    if fault in ('reservation', 'scope'):
        value[fault + '_sha256'] = '2' * 64
    elif fault == 'leaf':
        value['facts']['leaf']['inode'] += 1
    elif fault == 'extra':
        value['unknown'] = True
    elif fault == 'time':
        value['observed_ns'] = True
    else:
        value['observed_ns'] = time.monotonic_ns() + 10**12
    reader, writer = os.pipe()
    try:
        operation = getattr(gate, 'emit', None)
        assert callable(operation), 'startup emission must validate before writing'
        with pytest.raises(ValueError):
            operation(writer, handshake[3]['scope'], value)
        os.close(writer)
        writer = None
        assert os.read(reader, 4096) == b''
    finally:
        os.close(reader)
        if writer is not None:
            os.close(writer)
