"""CLAUDE.md sentinel-block writers never delete text outside their block.

Three writers replace a ``<!-- begin:NAME --> … <!-- end:NAME -->`` block in a
user-level CLAUDE.md: ``write_sentinel_block`` in
``scripts/lib/claude_md_blocks.sh`` (update.sh), the same function shipped into
the container by ``scripts/host-setup.sh``, and ``_replace_block`` in
``src/genesis/infra_profile/claude_md.py``. ONE fixture table drives the shell
and the Python implementation, so the rule cannot drift between them.

The defect this pins: a begin marker with no end made the shell writers' sed
range delete everything to the end of the file (the user's Personal Notes
included), and made the Python writer append a second block whose next run
deleted everything between the two.
"""

from __future__ import annotations

import os
import socket
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from genesis.infra_profile import claude_md

REPO = Path(__file__).resolve().parents[2]
LIB = REPO / "scripts" / "lib" / "claude_md_blocks.sh"
HOST_SETUP = REPO / "scripts" / "host-setup.sh"

NAME = "x"
B, E = f"<!-- begin:{NAME} -->", f"<!-- end:{NAME} -->"
NEW = "NEW"

# (case, file text, verdict, the non-block lines expected afterwards)
CASES = [
    ("one-pair", f"head\n{B}\nold\n{E}\nnotes\n", "ok", ["head\n", "notes\n"]),
    ("absent", "head\nnotes\n", "ok", ["head\n", "notes\n"]),
    ("empty-file", "", "ok", []),
    (
        "two-pairs-heal",
        f"a\n{B}\no1\n{E}\nmid\n{B}\no2\n{E}\ntail\n",
        "ok",
        ["a\n", "mid\n", "tail\n"],
    ),
    ("crlf", f"head\r\n{B}\r\nold\r\n{E}\r\nnotes\r\n", "ok", ["head\r\n", "notes\r\n"]),
    ("indented", f"head\n  {B}  \nold\n\t{E}\nnotes\n", "ok", ["head\n", "notes\n"]),
    ("mid-line-mention", f"see `{B}` here\nnotes\n", "ok", [f"see `{B}` here\n", "notes\n"]),
    ("orphan-begin", f"head\n{B}\nold\nPersonal notes\n", "refuse", None),
    ("orphan-end", f"head\nold\n{E}\nnotes\n", "refuse", None),
    ("end-before-begin", f"{E}\nmid\n{B}\n", "refuse", None),
    ("nested", f"{B}\n{B}\n{E}\n", "refuse", None),
    # Outside ASCII whitespace a line is NOT a marker, in both writers. The
    # Python writer once trimmed Unicode spaces, so this deleted "notes".
    ("nbsp-end-is-not-a-marker", f"h\n{B}\nold\nnotes\n\u00a0{E}\nn\n", "refuse", None),
    ("formfeed-is-space", f"h\n\f{B}\nold\n{E}\v\nn\n", "ok", ["h\n", "n\n"]),
]


POSIX_SPACE = " \t\n\v\f\r"  # awk's [[:space:]] in the C locale


def _lines(text: str) -> list[str]:
    parts = text.split("\n")
    return [f"{x}\n" for x in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _outside_block(text: str) -> list[str]:
    """The lines of ``text`` outside its block(s), split and trimmed as awk does."""
    out, inside = [], False
    for line in _lines(text):
        if line.strip(POSIX_SPACE) == B:
            inside = True
        elif line.strip(POSIX_SPACE) == E:
            inside = False
        elif not inside:
            out.append(line)
    return out


def _assert_one_fresh_block(text: str) -> None:
    lines = [ln.rstrip("\n") for ln in _lines(text)]
    trimmed = [ln.strip(POSIX_SPACE) for ln in lines]
    assert trimmed.count(B) == 1, text
    assert trimmed.count(E) == 1, text
    i = trimmed.index(B)
    assert lines[i + 1 : i + 3] == [NEW, E], text


def _shell(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "bash",
            "-c",
            f'. "{LIB}"; printf "{NEW}\\n" | write_sentinel_block "$1" {NAME}',
            "_",
            str(path),
        ],
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("phase", ["stdin", "begin", "body", "end", "rename"])
@pytest.mark.parametrize("context", ["guarded", "direct"])
@pytest.mark.parametrize("existing", [False, True])
def test_shell_io_failures_preserve_destination(tmp_path, phase, context, existing):
    path = tmp_path / "CLAUDE.md"
    original = f"head\n{B}\nold\n{E}\nPersonal Notes\nmine\n".encode()
    if existing:
        path.write_bytes(original)
    script = r'''
set -euo pipefail
source "$1"
phase=$3
mv() { if [[ "$phase" == rename ]]; then return 1; fi; command mv "$@"; }
cat() {
    if [[ "$phase" == stdin && $# == 0 ]]; then
        builtin printf PART
        return 1
    fi
    command cat "$@"
}
printf() {
    if [[ "$phase" == begin && "${2-}" == '<!-- begin:x -->' ||
          "$phase" == body && "$1" == '%s' && "${2-}" == $'BODY\n' ||
          "$phase" == end && "${2-}" == '<!-- end:x -->' ]]; then
        builtin printf PART
        return 1
    fi
    builtin printf "$@"
}
if [[ "$4" == guarded ]]; then
    if write_sentinel_block "$2" x <<< BODY; then exit 0; else exit $?; fi
else
    write_sentinel_block "$2" x <<< BODY
fi
'''
    result = subprocess.run(
        ["bash", "-c", script, "_", str(LIB), str(path), phase, context],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 1, result.stderr
    if phase != "rename":
        assert "left unchanged" in result.stderr
    if existing:
        assert path.read_bytes() == original
    else:
        assert not path.exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == (["CLAUDE.md"] if existing else [])


@pytest.mark.parametrize(("case", "text", "verdict", "kept"), CASES, ids=[c[0] for c in CASES])
def test_the_shell_writer(tmp_path, case, text, verdict, kept):
    path = tmp_path / "CLAUDE.md"
    path.write_bytes(text.encode())
    result = _shell(path)
    after = path.read_bytes().decode()
    if verdict == "refuse":
        assert result.returncode == 1, result.stderr
        assert after == text, "a refused file must be byte-for-byte untouched"
        assert "left unchanged" in result.stderr and "line" in result.stderr
    else:
        assert result.returncode == 0, result.stderr
        _assert_one_fresh_block(after)
        assert _outside_block(after) == kept
        assert after.endswith(f"{B}\n{NEW}\n{E}\n"), "the shell writer appends at EOF"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["CLAUDE.md"], "temp file left"


@pytest.mark.parametrize(("case", "text", "verdict", "kept"), CASES, ids=[c[0] for c in CASES])
def test_the_python_writer(case, text, verdict, kept):
    if verdict == "refuse":
        with pytest.raises(claude_md.MalformedBlockError, match="line"):
            claude_md._replace_block(text, NAME, NEW)
        return
    after = claude_md._replace_block(text, NAME, NEW)
    _assert_one_fresh_block(after)
    assert _outside_block(after) == kept


def test_the_python_writer_keeps_the_block_where_it_was():
    after = claude_md._replace_block(f"head\n{B}\nold\n{E}\nnotes\n", NAME, NEW)
    assert after == f"head\n{B}\n{NEW}\n{E}\nnotes\n"


def test_the_shell_writer_keeps_the_file_mode(tmp_path):
    path = tmp_path / "CLAUDE.md"
    path.write_text(f"{B}\nold\n{E}\n")
    path.chmod(0o644)
    assert _shell(path).returncode == 0
    assert stat.S_IMODE(path.stat().st_mode) == 0o644


def test_the_shell_writer_creates_a_missing_file(tmp_path):
    path = tmp_path / "CLAUDE.md"
    assert _shell(path).returncode == 0
    assert path.read_text() == f"{B}\n{NEW}\n{E}\n"


def test_the_cli_exits_non_zero_on_a_malformed_block(tmp_path, monkeypatch, capsys):
    """update.sh prints "refreshed" on exit 0, so a refusal must not exit 0."""
    from genesis.infra_profile import __main__ as cli
    from genesis.infra_profile import store

    md = tmp_path / "CLAUDE.md"
    original = "# mine\n<!-- begin:container-specs -->\nold\nPersonal notes\n"
    md.write_text(original)
    monkeypatch.setattr(claude_md, "CLAUDE_MD_PATH", md)
    monkeypatch.setattr(
        store,
        "load_profile",
        lambda *a, **k: {"sections": {"memory": {"status": "ok", "facts": {}, "metrics": {}}}},
    )
    monkeypatch.setattr(sys, "argv", ["genesis.infra_profile", "--claude-md-block"])
    assert cli.main() == 1
    assert md.read_text() == original
    assert "has no end" in capsys.readouterr().err


def _host_setup_writer_block() -> str:
    """The real host-setup.sh text that writes the container's block."""
    lines = HOST_SETUP.read_text().splitlines(keepends=True)
    start = next(
        i for i, ln in enumerate(lines) if ln.startswith('echo "$_NET_LINES" | incus exec')
    )
    end = next(
        i for i in range(start, len(lines)) if "WARN: Could not write network identity" in lines[i]
    )
    return "".join(lines[start : end + 1])


@pytest.mark.parametrize("malformed", [False, True])
def test_host_setup_ships_the_same_writer_into_the_container(tmp_path, malformed):
    """Run host-setup's own block-writing text, with a stub ``incus`` that runs
    the container command locally against a scratch HOME."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    md = home / ".claude" / "CLAUDE.md"
    original = (
        "# Install\n<!-- begin:network-identity -->\nold\n"
        + ("" if malformed else "<!-- end:network-identity -->\n")
        + "## Personal Notes\nmine\n"
    )
    md.write_text(original)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    stub = bindir / "incus"
    stub.write_text(
        "#!/bin/bash\n"
        "# Drop everything up to '--' (container, --user, --env), run the rest.\n"
        'while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done\n'
        "shift\n"
        f'HOME="{home}" exec "$@"\n'
    )
    stub.chmod(0o755)
    script = (
        "set -euo pipefail\n"  # host-setup.sh runs under these
        f'. "{LIB}"\n'
        "CONTAINER_NAME=c UBUNTU_UID=1000\n"
        '_NET_LINES="## Network Identity\n- **Container IP**: 192.0.2.10"\n'
        + _host_setup_writer_block()
    )
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}"}
    result = subprocess.run(["bash", "-c", script], capture_output=True, text=True, env=env)
    after = md.read_text()
    if malformed:
        assert after == original
        assert "WARN: Could not write network identity" in result.stdout
        assert "has no end" in result.stderr, "the refusal must reach the operator"
    else:
        assert "+ Network identity written" in result.stdout, result.stdout + result.stderr
        assert "192.0.2.10" in after and "old" not in after
        assert "## Personal Notes\nmine\n" in after


def _content(text: str) -> list[str]:
    return [ln.removesuffix("\n") for ln in _outside_block(text)]


def test_the_two_writers_agree_on_generated_files(tmp_path):
    """The fixture table is a sample; this is the class. Random files mixing
    markers with whitespace, CRs, form feeds, NBSP and Unicode line separators
    must get the same verdict and keep the same lines from both writers."""
    import random

    pool = [
        B,
        E,
        "keep",
        "",
        " " + B,
        "\t" + E,
        B + "\r",
        "\f" + E,
        "\v" + B,
        "\u00a0" + E,
        B + "\u00a0",
        "a\u2028" + B,
        "y\x85",
        "z\r",
        "\x1c" + E,
        f"text {B}",
        "<!-- begin:other -->",
    ]
    rng = random.Random(3135)
    script = (
        f'. "{LIB}"\n'
        'for f in "$@"; do printf "NEW\\n" | write_sentinel_block "$f" x 2>/dev/null; '
        'echo "$?"; done\n'
    )
    files, texts = [], []
    for i in range(400):
        text = "\n".join(rng.choice(pool) for _ in range(rng.randint(0, 6)))
        if rng.random() < 0.7:
            text += "\n"
        path = tmp_path / f"f{i}.md"
        path.write_bytes(text.encode())
        files.append(path)
        texts.append(text)
    out = subprocess.run(
        ["bash", "-c", script, "_", *map(str, files)], capture_output=True, text=True, check=True
    ).stdout.split()
    assert len(out) == len(files)
    disagreements = []
    for path, text, rc in zip(files, texts, out, strict=True):
        try:
            python_after = claude_md._replace_block(text, NAME, NEW)
            python_ok = True
        except claude_md.MalformedBlockError:
            python_ok = False
        shell_ok = rc == "0"
        if shell_ok != python_ok:
            disagreements.append((text, "shell" if shell_ok else "python", "accepted alone"))
        # The shell writer ends every kept line with a newline; the Python one
        # keeps a missing final newline as it was. The content must match.
        elif shell_ok and _content(path.read_bytes().decode()) != _content(python_after):
            disagreements.append((text, "kept lines differ"))
        elif not shell_ok and path.read_bytes().decode() != text:
            disagreements.append((text, "shell refused but changed the file"))
    assert not disagreements, disagreements[:5]


def test_a_name_with_a_backslash_matches_its_own_markers(tmp_path):
    """awk -v would turn ``a\\tb`` into a tab and append a block every run."""
    path = tmp_path / "CLAUDE.md"
    name = "a\\tb"
    path.write_text(f"<!-- begin:{name} -->\nold\n<!-- end:{name} -->\n")
    run = [
        "bash",
        "-c",
        f'. "{LIB}"; printf "NEW\\n" | write_sentinel_block "$1" "$2"',
        "_",
        str(path),
        name,
    ]
    assert subprocess.run(run, capture_output=True).returncode == 0
    assert subprocess.run(run, capture_output=True).returncode == 0
    assert path.read_text().count(f"<!-- begin:{name} -->") == 1


def test_an_unreadable_target_is_not_reported_as_a_marker_problem(tmp_path):
    target = tmp_path / "CLAUDE.md"
    target.mkdir()
    result = _shell(target)
    assert result.returncode == 1
    assert "could not read" in result.stderr
    assert "fix the markers" not in result.stderr


@pytest.mark.parametrize("kind", ["directory", "directory-symlink", "fifo", "socket"])
def test_nonregular_targets_are_refused_even_when_awk_skips_them(tmp_path, monkeypatch, kind):
    target = tmp_path / "CLAUDE.md"
    listener = None
    if kind == "directory":
        target.mkdir()
    elif kind == "directory-symlink":
        destination = tmp_path / "actual-directory"
        destination.mkdir()
        target.symlink_to(destination)
    elif kind == "fifo":
        os.mkfifo(target)
    else:
        listener = socket.socket(socket.AF_UNIX)
        listener.bind(str(target))
    # GNU awk skips directory operands with success; rejecting file types must
    # not depend on the chosen awk implementation (or block reading a FIFO).
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "awk").write_text("#!/bin/sh\nexit 0\n")
    (tools / "awk").chmod(0o755)
    monkeypatch.setenv("PATH", str(tools) + os.pathsep + os.environ["PATH"])
    before = target.lstat()
    try:
        result = _shell(target)
        assert result.returncode == 1
        assert "could not read" in result.stderr
        assert "fix the markers" not in result.stderr
        assert (target.lstat().st_ino, target.lstat().st_mode) == (before.st_ino, before.st_mode)
        assert not list(tmp_path.glob(".claude-md-block.*"))
        if target.is_dir():
            assert list(target.iterdir()) == []
    finally:
        if listener is not None:
            listener.close()
