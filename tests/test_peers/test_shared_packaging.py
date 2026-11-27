"""The distributed public identity reaches shared admission without checkout imports."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ASSET = "genesis/identity/PEER_PUBLIC.md"
ADMISSION = r"""
import asyncio
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import genesis
from genesis.peers import conversation as c

root = Path(sys.argv[1]).resolve()
assert Path(genesis.__file__).resolve().is_relative_to(root)
assert Path(c.__file__).resolve().is_relative_to(root)
assert c._PUBLIC_IDENTITY == root / "genesis/identity/PEER_PUBLIC.md"

class ReachedTransaction(Exception):
    pass

class Manager:
    async def effective_level(self, category):
        return 1

class Registry:
    calls = 0
    def transaction(self, **kwargs):
        self.calls += 1
        raise ReachedTransaction

async def run():
    database = object()
    runner = SimpleNamespace(_session_manager=SimpleNamespace(_db=database))
    runtime = SimpleNamespace(_autonomy_manager=Manager(), _db=database,
                              _direct_session_runner=runner)
    service = c.ConversationService(Registry(), runtime)
    identity = SimpleNamespace(peer={"same_owner": 0})
    incoming = SimpleNamespace(options={})
    original = c._PUBLIC_IDENTITY
    controls = root.parent / "identity-controls"
    controls.mkdir()
    cases = [("packaged", original, True)]
    for name, data in (("empty", b""), ("invalid-utf8", b"\xff")):
        path = controls / name
        path.write_bytes(data)
        cases.append((name, path, False))
    cases.append(("missing", controls / "absent", False))
    observed = []
    try:
        for name, path, valid in cases:
            c._PUBLIC_IDENTITY = path
            before = service.registry.calls
            try:
                await service._admit(identity, incoming, "synthetic", "digest", lambda: None)
            except ReachedTransaction:
                assert valid and service.registry.calls == before + 1
            except c.ConversationRefusal as error:
                assert not valid and error.code == "not_ready" and error.status == 503
                assert service.registry.calls == before
            else:
                raise AssertionError("Unexpected durable/native admission")
            observed.append(name)
    finally:
        c._PUBLIC_IDENTITY = original
    print(json.dumps(observed))

asyncio.run(run())
"""


def _python(code: str, cwd: Path, env: dict[str, str], *args: str, timeout: int) -> str:
    result = subprocess.run(
        [sys.executable, "-c", code, *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert result.returncode == 0, result.stdout + "\n" + result.stderr
    return result.stdout


def test_public_identity_artifacts_and_shared_admission(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    shutil.copytree(
        REPO / "src",
        source / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
    )
    shutil.copyfile(REPO / "pyproject.toml", source / "pyproject.toml")
    manifest = REPO / "MANIFEST.in"
    if manifest.is_file():
        shutil.copyfile(manifest, source / "MANIFEST.in")
    home, temporary, dist = (tmp_path / name for name in ("home", "temporary", "dist"))
    for path in (home, temporary, dist):
        path.mkdir()
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.update(HOME=str(home), GENESIS_HOME=str(home), TMPDIR=str(temporary))
    for name, directory in (
        ("XDG_CONFIG_HOME", ".config"),
        ("XDG_DATA_HOME", ".local/share"),
        ("XDG_CACHE_HOME", ".cache"),
    ):
        path = home / directory
        path.mkdir(parents=True, exist_ok=True)
        env[name] = str(path)
    for hook in ("build_wheel", "build_sdist"):
        _python(
            "import sys; import setuptools.build_meta as b; getattr(b, sys.argv[1])(sys.argv[2])",
            source,
            env,
            hook,
            str(dist),
            timeout=120,
        )
    (wheel,) = dist.glob("*.whl")
    (sdist,) = dist.glob("*.tar.gz")
    expected = (REPO / "src" / ASSET).read_bytes()
    wheel_root = tmp_path / "wheel-root"
    with zipfile.ZipFile(wheel) as wheel_archive:
        assert ASSET in wheel_archive.namelist(), "public identity missing from wheel"
        assert wheel_archive.namelist().count(ASSET) == 1
        assert wheel_archive.read(ASSET) == expected
        for member in wheel_archive.infolist():
            wheel_archive.extract(member, wheel_root)
    with tarfile.open(sdist) as sdist_archive:
        members = [
            member for member in sdist_archive.getmembers() if member.name.endswith("/src/" + ASSET)
        ]
        assert len(members) == 1, "public identity missing or duplicated in sdist"
        assert sdist_archive.extractfile(members[0]).read() == expected
    env["PYTHONPATH"] = str(wheel_root)
    observed = _python(ADMISSION, tmp_path, env, str(wheel_root), timeout=60)
    assert json.loads(observed) == ["packaged", "empty", "invalid-utf8", "missing"]
