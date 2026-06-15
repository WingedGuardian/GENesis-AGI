"""Tests for the guardian-gateway.sh ``update-cc`` op (WS-16).

The op installs a pinned Claude Code version on the host *under sudo*, so its
single argument is a security boundary: it is interpolated into a privileged
``npm install``. These tests run the REAL ``scripts/guardian-gateway.sh`` with
stubbed ``claude``/``npm``/``sudo``/``node`` on ``PATH`` so:

* the **accept** path is exercised end-to-end with zero real side effects
  (the stub npm only records its args), and
* every **malformed / injection** argument is rejected *before* any install is
  attempted (the stub npm is never invoked).
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

_GATEWAY = Path(__file__).resolve().parents[2] / "scripts" / "guardian-gateway.sh"


def _make_stub(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture
def stub_bin(tmp_path):
    """A bin dir with fake claude/npm/sudo/node; npm records its args to a file."""
    bind = tmp_path / "bin"
    bind.mkdir()
    record = tmp_path / "npm_args.txt"
    _make_stub(bind / "claude", "#!/usr/bin/env bash\necho '2.1.173 (Claude Code)'\n")
    _make_stub(bind / "node", "#!/usr/bin/env bash\necho 'v20.20.2'\n")
    # npm: record the args it was called with, then succeed.
    _make_stub(
        bind / "npm",
        f"#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >> '{record}'\nexit 0\n",
    )
    # sudo: strip leading flags (-n, etc.), then exec the remaining command.
    _make_stub(
        bind / "sudo",
        '#!/usr/bin/env bash\nwhile [ "${1:0:1}" = "-" ]; do shift; done\nexec "$@"\n',
    )
    return bind, record


def _run(ssh_command: str, stub_bin):
    bind, record = stub_bin
    env = dict(os.environ)
    env["PATH"] = f"{bind}:{env['PATH']}"
    # Passed as a literal env value — NOT through a shell — so metacharacters in
    # the argument are inert data, exactly as in a real forced-command session.
    env["SSH_ORIGINAL_COMMAND"] = ssh_command
    proc = subprocess.run(
        ["bash", str(_GATEWAY)],
        env=env,
        capture_output=True,
        text=True,
    )
    npm_calls = record.read_text() if record.exists() else ""
    return proc, npm_calls


def test_update_cc_valid_version_installs_pinned_package(stub_bin):
    """A valid semver installs the hardcoded package at that exact version."""
    proc, npm_calls = _run("update-cc 2.1.173", stub_bin)
    assert proc.returncode == 0, proc.stderr
    assert '"ok": true' in proc.stdout
    # Exact package, exact version — the package name is never derived from input.
    assert "install -g @anthropic-ai/claude-code@2.1.173" in npm_calls


@pytest.mark.parametrize(
    "bad",
    [
        "1.2",                       # too few components
        "1.2.3.4",                   # too many components
        "v2.1.173",                  # leading v
        "latest",                    # dist-tag, not semver
        "2.1.173-beta",              # prerelease suffix
        "",                          # empty arg
        "2.1.173; rm -rf /tmp/x",    # command chaining via ;
        "2.1.173 && touch /tmp/x",   # command chaining via &&
        "$(touch /tmp/x)",           # command substitution
        "2.1.173|whoami",            # pipe
        "../evil",                   # path traversal junk
        "2.1.173 ",                  # trailing space (regex is anchored at $)
        " 2.1.173",                  # leading space (e.g. double space after update-cc)
    ],
)
def test_update_cc_rejects_malformed_or_injection(bad, stub_bin):
    """Anything that is not a bare X.Y.Z is rejected before npm ever runs."""
    proc, npm_calls = _run(f"update-cc {bad}", stub_bin)
    assert proc.returncode != 0
    assert npm_calls == "", f"npm must NOT run for rejected input {bad!r}"


def test_update_cc_bare_command_is_denied(stub_bin):
    """`update-cc` with no argument (no trailing space) falls through to the deny
    default — it must not match the `update-cc *` arm or run npm."""
    proc, npm_calls = _run("update-cc", stub_bin)
    assert proc.returncode != 0
    assert "denied" in proc.stderr
    assert npm_calls == ""
