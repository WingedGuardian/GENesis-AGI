"""The corpus cache holds real commands, so its file mode is a security control.

``scripts/replay_guard_corpus.py`` caches every distinct Bash command this
install has ever run, verbatim, so it can replay them through a guard. That
file demonstrably contains secrets passed in argv (an inline ``SSHPASS=`` was
found in it), which is why it is written 0600 and lives outside the repo.

The mode was being requested in a way that silently did not apply. ``os.open``
honours its mode argument ONLY on the call that actually creates the file, so a
``--rebuild`` over a cache already sitting at 0644 rewrote the secrets into a
world-readable inode — while the confirmation line printed ``(mode 0600)``,
because that string was hard-coded rather than measured. The tightening helper
existed but was wired only to the LOAD path, never the rebuild path.

Both halves are locked here: the inode is tightened on rebuild, and the mode
the tool announces is read back off the file rather than asserted.
"""

from __future__ import annotations

import importlib.util
import os
import stat
import sys
import types
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _REPO_ROOT / "scripts" / "replay_guard_corpus.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("replay_guard_corpus", _SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    # Registered before exec so the module's own `from __future__`/dataclass
    # machinery resolves normally, matching how the script runs as __main__.
    sys.modules["replay_guard_corpus"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def rgc():
    return _load_module()


_ROWS = [("echo one", "/tmp"), ("echo two", "/tmp")]


@pytest.fixture
def cache(tmp_path, rgc, monkeypatch):
    """Point the module at a throwaway cache and stub the 1.4 GB transcript walk."""
    path = tmp_path / "guard-corpus.jsonl"
    monkeypatch.setattr(rgc, "_CACHE", path)
    monkeypatch.setattr(rgc, "_extract_commands", lambda: list(_ROWS))
    return path


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_rebuild_over_a_world_readable_cache_tightens_the_inode(cache, rgc):
    """The regression: rebuilding onto an existing 0644 file left it 0644.

    O_CREAT's mode is not applied when the file already exists, so the secrets
    were rewritten into an inode anyone on the box could read.
    """
    cache.write_text('"echo stale"\n')
    cache.chmod(0o644)

    rgc.load_corpus(rebuild=True)

    assert _mode(cache) == 0o600, (
        "rebuild left the corpus cache at "
        f"{_mode(cache):04o}; it holds verbatim commands including secrets"
    )


def test_a_freshly_created_cache_is_never_world_readable(cache, rgc):
    """The create path, which was already correct — pinned so it stays that way."""
    assert not cache.exists()

    rgc.load_corpus(rebuild=True)

    assert _mode(cache) == 0o600


def test_the_announced_mode_is_measured_not_asserted(cache, rgc, monkeypatch, capsys):
    """The confirmation line must report the mode the file HAS, not a constant.

    A hard-coded ``(mode 0600)`` is what kept the bug invisible for the whole
    life of the script: the one line a reader would check said the right thing
    while the file said something else. Defeating the tightening proves the
    message tracks the file rather than the intent — with the real
    :func:`os.fchmod` in place both values are 0600 and any string would pass.
    """
    cache.write_text('"echo stale"\n')
    cache.chmod(0o644)
    monkeypatch.setattr(rgc.os, "fchmod", lambda *a, **k: None)

    rgc.load_corpus(rebuild=True)

    err = capsys.readouterr().err
    assert _mode(cache) == 0o644, "the fchmod stub did not take effect"
    assert "(mode 0644)" in err, f"announced mode does not match the file: {err!r}"


def test_load_path_tightens_a_world_readable_cache(cache, rgc):
    """Reading a cache written before the 0600 default also repairs it."""
    cache.write_text('["echo one", "/tmp"]\n')
    cache.chmod(0o644)

    assert rgc.load_corpus() == [("echo one", "/tmp")]

    assert _mode(cache) == 0o600


def test_a_cache_without_the_cwd_field_is_rebuilt_not_replayed(cache, rgc, capsys):
    """A v1 cache holds bare command strings — no cwd.

    Replaying those means replaying every command from the repo root, which is
    the invalid measurement the format change exists to fix: a guard that asks
    "am I inside a worktree?" answers "no" every time and the rate silently
    under-counts. Accepting a v1 line for compatibility would restore that with
    every other test still green, so the loader must rebuild instead.
    """
    cache.write_text('"echo legacy"\n')

    result = rgc.load_corpus()

    assert result == _ROWS, "a v1 cache was replayed instead of rebuilt"
    assert "rebuilding" in capsys.readouterr().err


def test_each_command_is_replayed_from_the_directory_it_was_typed_in(cache, rgc):
    """The cwd reaches BOTH the payload and the process.

    The Python guards call os.getcwd() directly rather than reading the
    payload's cwd field, so threading it into the payload alone would look
    correct and change nothing. Asserting both is what makes the fix real.
    """
    seen = {}

    def fake_main():
        seen["process_cwd"] = os.getcwd()
        seen["payload_cwd"] = mod.read_payload()["cwd"]
        return 0

    mod = types.SimpleNamespace(main=fake_main, read_payload=lambda: None)
    rgc._run_python_guard._loaded["fake_guard"] = mod

    before = os.getcwd()
    rgc._run_python_guard("fake_guard", "echo hi", "/tmp")

    assert seen["process_cwd"] == "/tmp", "the process cwd did not move"
    assert seen["payload_cwd"] == "/tmp", "the payload cwd did not move"
    assert os.getcwd() == before, "the replay left the process in the wrong directory"


def test_a_vanished_directory_is_substituted_and_counted(cache, rgc):
    """Honesty valve: a recorded cwd that no longer exists falls back to the
    repo root, and the count of those is reported rather than folded silently
    into the rate."""
    rgc._SUBSTITUTED_CWD["n"] = 0

    resolved = rgc._effective_cwd("/nonexistent/directory/from/an/old/session")

    assert resolved == str(rgc._REPO)
    assert rgc._SUBSTITUTED_CWD["n"] == 1
