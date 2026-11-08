"""Actual entrypoint normalization must retain canonical argv and lock identity."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from tests.test_scripts.test_code_intel_index import (
    _SYSTEM_PATH,
    _fake_tools,
    _run_entry,
    _write_exec,
)


@pytest.mark.parametrize("alias", [False, True])
@pytest.mark.parametrize("leading_slashes", [1, 2, 3])
@pytest.mark.parametrize(
    "name", ["plain", "tab\trepo", "embedded\nrepo", "trailing\n", "two\n\n", "-leading"]
)
def test_actual_entrypoint_retains_physical_path_before_lock_and_provider(
    tmp_path, name, alias, leading_slashes
):
    repo = tmp_path / name
    repo.mkdir()
    (repo / ".git").mkdir()
    stripped = Path(str(repo).rstrip("\n"))
    if stripped != repo:
        stripped.mkdir()
        (stripped / ".git").mkdir()
    spelling = repo.name
    if alias:
        link = tmp_path / "alias"
        link.symlink_to(repo, target_is_directory=True)
        spelling = link.name
    if leading_slashes > 1:
        spelling = "/" * leading_slashes + str(tmp_path / spelling).lstrip("/")
    bindir, log = tmp_path / "bin", tmp_path / "calls.jsonl"
    _fake_tools(bindir, tmp_path / "fixture.log")
    _write_exec(
        bindir / "gitnexus",
        "#!/usr/bin/python3\n"
        "import json,os,sys\n"
        "from pathlib import Path\n"
        "if sys.argv[1:]==['--version']:\n"
        "    print(os.environ.get('FAKE_GITNEXUS_VERSION','1.6.12'));sys.exit(0)\n"
        f"with Path({str(log)!r}).open('a') as stream:\n"
        "    stream.write(json.dumps({'cwd':os.getcwd(),'argv':sys.argv[1:]})+'\\n')\n",
    )
    result = _run_entry(
        tmp_path,
        spelling,
        "gitnexus",
        "fast",
        path=str(bindir) + os.pathsep + _SYSTEM_PATH,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert log.exists(), result.stdout + result.stderr
    assert [json.loads(line) for line in log.read_text().splitlines()] == [
        {"cwd": os.path.realpath(repo), "argv": ["analyze"]}
    ]
    h = hashlib.sha1(os.fsencode(os.path.realpath(repo))).hexdigest()[:16]
    locks = tmp_path / ".genesis/locks"
    assert (locks / f"code-intel-{h}.lock").exists()
    if stripped != repo:
        wrong = hashlib.sha1(os.fsencode(os.path.realpath(stripped))).hexdigest()[:16]
        assert wrong != h and not (locks / f"code-intel-{wrong}.lock").exists()
