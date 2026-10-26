"""Resource readiness works above select's fixed descriptor range."""
import fcntl
import os
from types import SimpleNamespace

import pytest

from genesis.transcript_analytics import resources


@pytest.mark.parametrize("minimum", [0, 1100])
@pytest.mark.parametrize("ack", [b"1", b"0", None])
def test_ready_descriptor(minimum, ack):
    reader,writer=os.pipe()
    high=fcntl.fcntl(reader,fcntl.F_DUPFD,minimum)
    os.close(reader)
    try:
        if ack is None:
            os.close(writer)
            writer = -1
        else:
            os.write(writer,ack)
        owner=SimpleNamespace(checkpoint=lambda:None,signals=0,exited=lambda:False)
        assert resources._await_ready(owner,high) is (ack == b"1")
    finally:
        os.close(high)
        if writer != -1:
            os.close(writer)


@pytest.mark.parametrize("minimum", [0, 1100])
def test_ready_cancellation_preserves_buffered_ack(minimum):
    reader, writer = os.pipe()
    descriptor = fcntl.fcntl(reader, fcntl.F_DUPFD, minimum)
    os.close(reader)
    try:
        os.write(writer, b"1")
        owner = SimpleNamespace(checkpoint=lambda: None, signals=1,
                                exited=lambda: pytest.fail("must not consume child status"))
        assert resources._await_ready(owner, descriptor) is False
        assert os.read(descriptor, 1) == b"1"
    finally:
        os.close(descriptor)
        os.close(writer)
