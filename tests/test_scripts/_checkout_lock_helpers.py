from __future__ import annotations

import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


@contextmanager
def held(path: Path, mode: int):
    code = "import fcntl,sys; f=open(sys.argv[1],'a+'); fcntl.flock(f,int(sys.argv[2])); print('held',flush=True); sys.stdin.read()"
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(path), str(mode)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "held"
        yield
    finally:
        proc.stdin.close()
        proc.wait(timeout=5)


def can_lock(path: Path, mode: int) -> bool:
    code = "import fcntl,os,sys; f=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600); fcntl.flock(f,int(sys.argv[2])|fcntl.LOCK_NB)"
    return (
        subprocess.run(
            [sys.executable, "-c", code, str(path), str(mode)], check=False
        ).returncode
        == 0
    )
