"""Pinned app-server skill discovery, with an owned home and no model turn."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.test_hooks.native_codex import AppServer

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.skipif(
    os.environ.get("GENESIS_CODEX_NATIVE_TESTS") != "1", reason="opt-in pinned native skill discovery",
)


def test_actual_pinned_client_discovers_single_source_symlink(tmp_path):
    binary = os.environ["GENESIS_CODEX_CLI_BIN"]
    assert subprocess.check_output([binary, "--version"], text=True).strip() == "codex-cli 0.163.0"
    assert hashlib.sha256(Path(binary).read_bytes()).digest() == bytes.fromhex(
        "399ff69c 431c066e 40de318f ba45c2b0 7f45a76b 19939d24 7ee4828f c70c4215"
    )
    home = tmp_path / "codex"
    home.mkdir(mode=0o700)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env = {"HOME": str(tmp_path), "CODEX_HOME": str(home), "PATH": os.defpath}
    subprocess.run(["git", "init", "-q", str(workspace)], env=env, check=True)
    (home / "config.toml").write_text(
        'model="unused-owned-model"\napproval_policy="never"\nsandbox_mode="danger-full-access"\n'
        '[analytics]\nenabled=false\n[features]\nplugins=false\nremote_skills=false\n[agents]\nenabled=false\n'
    )
    source = ROOT / ".agents/skills/validating-merges"
    assert source.is_symlink()
    driver = AppServer(binary, tmp_path, env)
    evidence = {"source_sha256": hashlib.sha256((source / "SKILL.md").read_bytes()).hexdigest()}
    try:
        driver.initialize()
        before = driver.request("skills/list", {"cwds": [str(workspace)], "forceReload": True}, 2)
        assert not any(s["name"] == "validating-merges" for row in before["data"] for s in row["skills"])
        canonical = workspace / ".claude/skills/validating-merges"
        shutil.copytree(source.resolve(), canonical)
        link = workspace / ".agents/skills/validating-merges"
        link.parent.mkdir(parents=True)
        link.symlink_to(source.readlink(), target_is_directory=True)
        after = driver.request("skills/list", {"cwds": [str(workspace)], "forceReload": True}, 3)
        rows = [row for row in after["data"] if Path(row["cwd"]) == workspace]
        assert len(rows) == 1 and not rows[0]["errors"]
        found = [s for s in rows[0]["skills"] if s["name"] == "validating-merges"]
        assert len(found) == 1 and found[0]["enabled"] is True and found[0]["scope"] == "repo"
        loaded = Path(found[0]["path"])
        assert loaded.resolve() == canonical / "SKILL.md"
        assert hashlib.sha256(loaded.read_bytes()).hexdigest() == evidence["source_sha256"]
        evidence.update(before=before, after=after)
        assert not (home / "auth.json").exists()
    finally:
        evidence["events"] = driver.events
        driver.close()
        (tmp_path / "native-skill-evidence.json").write_text(json.dumps(evidence, indent=2))
