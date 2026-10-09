"""Cross-process lock order: writer, then snapshot publication."""

import contextlib
import fcntl

from genesis.env import genesis_home

PUBLICATION_LOCK = genesis_home() / "locks/transcript-analytics-publication.lock"


def restore_epoch():
    try:
        return (PUBLICATION_LOCK.parent / "transcript-analytics-restore-epoch").read_text()
    except FileNotFoundError:
        return None


@contextlib.contextmanager
def publication(*, exclusive=False):
    PUBLICATION_LOCK.parent.mkdir(parents=True, exist_ok=True)
    with PUBLICATION_LOCK.open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield
