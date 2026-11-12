"""scripts/cc_plugins.py: registering Genesis's Claude Code plugins, against a fake `claude` CLI.

The fake keeps the marketplace and plugin state in a JSON file and models the behaviour measured on the real CLI
(2.1.299): `install` on an installed plugin re-enables it, `marketplace remove` uninstalls the marketplace's
plugins. The real CLI was also exercised end to end in a scratch config directory for the PR's Testing section.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "cc_plugins.py"
PLUGIN = "genesis-secret-redact@genesis"

FAKE_CLAUDE = r"""#!/usr/bin/env python3
import json, os, sys
state_file = os.environ["FAKE_CLAUDE_STATE"]
st = json.load(open(state_file)) if os.path.exists(state_file) else {"mkts": {}, "plugins": {}}
calls = os.environ.get("FAKE_CLAUDE_CALLS")
if calls:
    open(calls, "a").write(" ".join(sys.argv[1:]) + "\n")
if os.environ.get("FAKE_CLAUDE_FAIL") and sys.argv[2:3] == [os.environ["FAKE_CLAUDE_FAIL"]]:
    print("simulated failure", file=sys.stderr); sys.exit(1)
a = sys.argv[2:]
if os.environ.get("FAKE_CLAUDE_FAIL_IF_ARG") and os.environ["FAKE_CLAUDE_FAIL_IF_ARG"] in " ".join(a):
    print("simulated failure", file=sys.stderr); sys.exit(1)
def save(): json.dump(st, open(state_file, "w"))
if a == ["marketplace", "list", "--json"]:
    print(json.dumps([{"name": n, "source": "directory", "path": p} for n, p in st["mkts"].items()]))
elif a == ["list", "--json"]:
    print(json.dumps([{"id": i, "enabled": e} for i, e in st["plugins"].items()]))
elif a[:2] == ["marketplace", "add"]:
    name = json.load(open(os.path.join(a[2], ".claude-plugin", "marketplace.json")))["name"]
    st["mkts"][name] = a[2]; save()
elif a[:2] == ["marketplace", "remove"]:
    st["mkts"].pop(a[2], None)
    st["plugins"] = {i: e for i, e in st["plugins"].items() if not i.endswith("@" + a[2])}; save()
elif a[0] == "install":
    if a[1].split("@")[1] not in st["mkts"]:
        print("unknown marketplace", file=sys.stderr); sys.exit(1)
    st["plugins"][a[1]] = True; save()
elif a[0] == "uninstall":
    st["plugins"].pop(a[1], None); save()
elif a[0] in ("disable", "enable"):
    st["plugins"][a[1]] = a[0] == "enable"; save()
else:
    print("unexpected: " + " ".join(a), file=sys.stderr); sys.exit(1)
"""


@pytest.fixture
def world(tmp_path):
    """A main checkout with plugins/, a fake claude on PATH, a scratch GENESIS_HOME."""
    root = tmp_path / "repo"
    (root / "plugins" / ".claude-plugin").mkdir(parents=True)
    (root / "plugins" / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps(
            {
                "name": "genesis",
                "plugins": [{"name": "genesis-secret-redact", "source": "./genesis-secret-redact"}],
            }
        )
    )
    (root / "plugins" / "genesis-secret-redact" / ".claude-plugin").mkdir(parents=True)
    (root / "plugins" / "genesis-secret-redact" / ".claude-plugin" / "plugin.json").write_text(
        '{"name": "genesis-secret-redact"}'
    )
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "claude"
    fake.write_text(FAKE_CLAUDE.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
    fake.chmod(0o755)
    home = tmp_path / "genesis-home"
    (home / "config").mkdir(parents=True)
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "FAKE_CLAUDE_STATE": str(tmp_path / "state.json"),
        "FAKE_CLAUDE_CALLS": str(tmp_path / "calls.log"),
        "GENESIS_HOME": str(home),
    }

    class World:
        def run(self, mode, *, repo=root, **extra):
            return subprocess.run(
                [sys.executable, str(SCRIPT), mode, "--repo-root", str(repo)],
                capture_output=True,
                text=True,
                env={**env, **extra},
                timeout=60,
            )

        def state(self):
            return json.loads((tmp_path / "state.json").read_text())

        def set_state(self, value):
            (tmp_path / "state.json").write_text(json.dumps(value))

        def lever(self, value):
            (home / "config" / "genesis.yaml").write_text(
                f"cc_plugins:\n  secret_redact: {value}\n"
            )

        def calls(self):
            log = tmp_path / "calls.log"
            return log.read_text().splitlines() if log.exists() else []

    w = World()
    w.root, w.tmp, w.env = root, tmp_path, env
    return w


def test_fresh_install_registers_the_marketplace_and_installs(world):
    run = world.run("apply")
    assert run.returncode == 0
    st = world.state()
    assert st["mkts"] == {"genesis": str((world.root / "plugins").resolve())}
    assert st["plugins"] == {PLUGIN: True}
    assert world.run("check").returncode == 0


def test_apply_is_idempotent(world):
    world.run("apply")
    before = len(world.calls())
    run = world.run("apply")
    assert "up to date" in run.stdout
    writes = [c for c in world.calls()[before:] if "--json" not in c]
    assert writes == []


def test_owner_disabled_plugin_is_never_re_enabled(world):
    world.run("apply")
    world.set_state({**world.state(), "plugins": {PLUGIN: False}})
    before = len(world.calls())
    run = world.run("apply")
    assert run.returncode == 0 and world.state()["plugins"] == {PLUGIN: False}
    assert not any(c.startswith("plugin install") for c in world.calls()[before:])
    chk = world.run("check")
    assert chk.returncode == 3 and "disabled while the lever is on" in chk.stdout


def test_moved_checkout_is_re_pointed_and_keeps_a_disabled_plugin_disabled(world):
    world.set_state({"mkts": {"genesis": "/old/checkout/plugins"}, "plugins": {PLUGIN: False}})
    run = world.run("apply")
    assert run.returncode == 0 and "re-pointed" in run.stdout
    st = world.state()
    assert st["mkts"]["genesis"] == str((world.root / "plugins").resolve())
    assert st["plugins"] == {PLUGIN: False}


def test_lever_off_uninstalls_and_check_agrees(world):
    world.run("apply")
    world.lever("off")
    assert world.run("apply").returncode == 0
    assert world.state()["plugins"] == {}
    assert world.run("check").returncode == 0


def test_invalid_lever_changes_nothing(world):
    world.lever("maybe")
    run = world.run("apply")
    assert "not on/off" in run.stdout and not (world.tmp / "state.json").exists()
    assert world.run("check").returncode == 3


@pytest.mark.parametrize(
    ("yaml_text", "apply_rc", "check_rc", "said"),
    [
        (
            "cc_plugins: off\n",
            0,
            3,
            "not on/off",
        ),  # one switch for all plugins: a likely owner mistake
        ("cc_plugins: [\n", 2, 2, "not valid YAML"),
        (
            "- just\n- a list\n",
            0,
            0,
            None,
        ),  # not a mapping: the default (on) applies, so check agrees
    ],
)
def test_malformed_lever_never_crashes(world, yaml_text, apply_rc, check_rc, said):
    (world.tmp / "genesis-home" / "config" / "genesis.yaml").write_text(yaml_text)
    run = world.run("apply")
    assert run.returncode == apply_rc and "Traceback" not in run.stdout + run.stderr
    if said:
        assert said in run.stdout
    chk = world.run("check")
    assert chk.returncode == check_rc and "Traceback" not in chk.stdout + chk.stderr


def test_a_failed_re_point_says_redaction_is_off(world):
    world.set_state({"mkts": {"genesis": "/old/checkout/plugins"}, "plugins": {PLUGIN: True}})
    run = world.run("apply", FAKE_CLAUDE_FAIL="install")
    assert run.returncode == 2 and "NOT INSTALLED" in run.stdout


def test_a_failed_re_point_restores_the_previous_registration(tmp_path, world):
    old = tmp_path / "old-checkout" / "plugins"
    (old / ".claude-plugin").mkdir(parents=True)
    (old / ".claude-plugin" / "marketplace.json").write_text(
        json.dumps({"name": "genesis", "plugins": []})
    )
    world.set_state({"mkts": {"genesis": str(old)}, "plugins": {PLUGIN: False}})
    run = world.run("apply", FAKE_CLAUDE_FAIL_IF_ARG=str(world.root / "plugins"))
    assert run.returncode == 2 and "restored the previous registration" in run.stdout
    assert world.state() == {"mkts": {"genesis": str(old)}, "plugins": {PLUGIN: False}}


def test_bootstrap_registers_plugins_only_after_the_worktree_guard():
    text = (REPO / "scripts" / "bootstrap.sh").read_text()
    assert text.index("editable_install_guarded") < text.index('cc_plugins.py" apply')


def test_linked_worktree_is_refused(world):
    subprocess.run(
        ["git", "-C", str(world.root), "commit", "-q", "--allow-empty", "-m", "init"],
        check=True,
        env={
            **world.env,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    )
    wt = world.tmp / "wt"
    subprocess.run(["git", "-C", str(world.root), "worktree", "add", "-q", str(wt)], check=True)
    run = world.run("apply", repo=wt)
    assert run.returncode == 2 and "linked worktree" in run.stdout
    assert not (world.tmp / "state.json").exists()


def test_no_claude_on_path_is_reported_not_fatal(world):
    run = world.run("apply", PATH="/nonexistent")
    assert run.returncode == 2 and "not on PATH" in run.stdout


def test_a_cli_failure_is_reported_and_exits_2(world):
    run = world.run("apply", FAKE_CLAUDE_FAIL="install")
    assert run.returncode == 2 and "could not apply" in run.stdout


def test_check_flags_a_missing_plugin_folder(world):
    world.run("apply")
    (world.root / "plugins" / "genesis-secret-redact" / ".claude-plugin" / "plugin.json").unlink()
    chk = world.run("check")
    assert chk.returncode == 3 and "plugin folder is missing" in chk.stdout


def test_bootstrap_and_update_apply_the_plugins():
    for script in ("bootstrap.sh", "update.sh"):
        text = (REPO / "scripts" / script).read_text()
        assert 'cc_plugins.py" apply --repo-root "$GENESIS_ROOT"' in text, script


def test_marketplace_lists_only_the_redaction_plugin():
    manifest = json.loads((REPO / "plugins" / ".claude-plugin" / "marketplace.json").read_text())
    assert manifest["name"] == "genesis"
    assert [p["name"] for p in manifest["plugins"]] == ["genesis-secret-redact"]
