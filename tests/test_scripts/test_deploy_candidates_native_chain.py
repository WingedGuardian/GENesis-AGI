"""Candidate readiness and restoration include the installed native chain."""
from __future__ import annotations

import hashlib
import json
import subprocess
import sys

import pytest

from tests.test_scripts._deploy_candidates_world import CHAIN_HELPERS


@pytest.mark.parametrize("damage", [
    "manifest_missing", "manifest_invalid", "manifest_version", "prepared", "unenrolled",
    "wrong_root", "wrapper_hash", "helper_hash", "helper_omitted", "helper_missing",
    "helper_edited", "helper_not_executable", "original_missing", "original_edited", "original_mode", "custom_original",
    "unrecorded_original", "unrecorded_original_symlink",
])
def test_readiness_refuses_incomplete_native_chain(dc, dc_ready, damage):
    w = dc_ready
    hooks = w.root / ".git/hooks"
    record = hooks / ".genesis-pre-push.json"
    state = json.loads(record.read_text())
    if damage == "manifest_missing":
        record.unlink()
    elif damage == "manifest_invalid":
        record.write_text("[]")
    else:
        if damage == "manifest_version":
            state["version"] = 2
        elif damage == "prepared":
            state["phase"] = "prepared"
        elif damage == "unenrolled":
            state["repos"] = {}
        elif damage == "wrong_root":
            state["repos"][str(w.root / ".git")] = str(w.tmp / "other")
        elif damage == "wrapper_hash":
            state["wrapper_sha256"] = "0" * 64
        elif damage == "helper_hash":
            state["helpers"]["pre_push_gates.sh"] = "0" * 64
        elif damage == "helper_omitted":
            state["helpers"].pop("pre_push_gates.sh")
        elif damage in ("helper_missing", "helper_edited", "helper_not_executable"):
            helper = hooks / "pre_push_gates.sh"
            if damage == "helper_missing":
                helper.unlink()
            elif damage == "helper_edited":
                helper.write_text("#!/bin/sh\nexit 1\n")
            else:
                helper.chmod(0o644)
        elif damage.startswith("unrecorded_original"):
            original = hooks / ".genesis-pre-push-original"
            if damage.endswith("symlink"):
                original.symlink_to(hooks / "absent")
            else:
                original.write_text("unrecorded preservation bytes")
        else:
            original = hooks / ".genesis-pre-push-original"
            original.write_text("#!/bin/sh\nexit 0\n")
            original.chmod(0o755)
            state["original"] = {"sha256": hashlib.sha256(original.read_bytes()).hexdigest(), "mode": 0o755}
            state["run_original_for_enrolled"] = damage == "custom_original"
            if damage == "original_missing":
                original.unlink()
            elif damage == "original_edited":
                original.write_text("changed")
            elif damage == "original_mode":
                original.chmod(0o644)
        record.write_text(json.dumps(state))
    before = w.rev("HEAD")
    assert dc.gate.hook_install_failures(dc.Engine(w.root, w.env, w.gh, w.serving))
    assert w.rev("HEAD") == before


def test_real_owned_native_chain_is_ready_and_runs(dc, dc_ready):
    w = dc_ready
    assert dc.gate.hook_install_failures(dc.Engine(w.root, w.env, w.gh, w.serving)) == []
    result = subprocess.run(
        [sys.executable, str(w.root / ".git/hooks/pre-push"), "origin", "synthetic"],
        input=b"", cwd=w.root, env=w.env, capture_output=True,
    )
    assert result.returncode == 0, result.stderr


def test_helper_only_candidate_restores_owned_chain_and_refuses_off_live_drop(dc, dc_ready, capsys):
    w = dc_ready
    path = "scripts/hooks/pre_push_gates.sh"
    base = (w.root / path).read_text()
    changed = base + "# approved candidate\n"
    w.candidate("feat/native-helper", {path: changed})
    assert w.add(dc, "feat/native-helper", "--approve-hooks", "owner, in chat") == 0
    assert w.run(dc, "rebuild") == 0, capsys.readouterr()
    assert (w.root / ".git/hooks/pre_push_gates.sh").read_text() == changed
    assert dc.gate.hook_install_failures(dc.Engine(w.root, w.env, w.gh, w.serving)) == []
    w.git(w.root, "switch", "-q", "main")
    assert w.run(dc, "drop", "feat/native-helper") == 1
    assert "stay installed" in capsys.readouterr().err
    w.git(w.root, "switch", "-q", "live")
    assert w.run(dc, "drop", "feat/native-helper") == 0, capsys.readouterr()
    assert (w.root / ".git/hooks/pre_push_gates.sh").read_text() == base
    assert dc.gate.hook_install_failures(dc.Engine(w.root, w.env, w.gh, w.serving)) == []


@pytest.mark.parametrize("history", ["prior_record", "first_install", "unqualified_legacy", "unqualified_with_record"])
def test_chain_survives_legacy_rollback_and_reupgrade(dc, dc_ready, capsys, history):
    """Legacy readers restore files without knowing the private chain record."""
    w = dc_ready
    chain_tip = w.rev("HEAD")
    sync = (w.root / "scripts/hooks/sync-hooks.sh").read_text()
    arrays = sync[sync.index("HOOKS_TO_SYNC=("):sync.index("MODIFIED_FOUND=0")]
    for name in CHAIN_HELPERS:
        arrays = arrays.replace(f'    "{name}"\n', "")
    legacy_hook = (w.root / "scripts/hooks/pre_push_gates.sh").read_text()
    if history.startswith("unqualified"):
        legacy_hook += "# a prior source, but neither target gates nor shipped\n"
    legacy = w.candidate("feat/legacy", {
        "scripts/hooks/sync-hooks.sh": "#!/bin/bash\n" + arrays + "exit 0\n",
        "scripts/hooks/pre-push": legacy_hook,
        **{f"scripts/hooks/{name}": None for name in CHAIN_HELPERS},
    })
    w.git(w.root, "-c", "core.hooksPath=/dev/null", "switch", "-q", "-C", "live", legacy)
    assert w.run(dc, "restore-hooks", chain_tip, legacy, lock=True) == 0, capsys.readouterr()
    hooks = w.root / ".git/hooks"
    assert (hooks / ".genesis-pre-push.json").exists()
    assert not (hooks / "pre_push_chain.py").exists()
    if history in ("first_install", "unqualified_legacy"):
        (hooks / ".genesis-pre-push.json").unlink()
    w.git(w.root, "-c", "core.hooksPath=/dev/null", "switch", "-q", "-C", "live", chain_tip)
    result = w.run(dc, "restore-hooks", legacy, chain_tip, lock=True)
    if history.startswith("unqualified"):
        assert result == 1
        assert "not a qualified managed version" in capsys.readouterr().err
        assert (hooks / "pre-push").read_text() == legacy_hook
        assert not (hooks / "pre_push_chain.py").exists()
        assert (hooks / ".genesis-pre-push.json").exists() is (history == "unqualified_with_record")
        return
    assert result == 0, capsys.readouterr()
    assert dc.gate.hook_install_failures(dc.Engine(w.root, w.env, w.gh, w.serving)) == []
    if history == "first_install":
        assert (hooks / ".genesis-pre-push-original").read_text() == legacy_hook


def test_restore_does_not_recreate_a_missing_active_chain_helper(dc, dc_ready, capsys):
    w = dc_ready
    helper = w.root / ".git/hooks/pre_push_gates.sh"
    helper.unlink()
    head = w.rev("HEAD")
    assert w.run(dc, "restore-hooks", head, head, lock=True) == 1, capsys.readouterr()
    assert not helper.exists(), "ordinary active-state damage became a downgrade recovery"


def _expanded_chain_changes(w):
    path = "scripts/hooks/pre_push_chain.py"
    source = (w.root / path).read_text()
    before = next(line for line in source.split("\n") if line.startswith("HELPERS = "))
    assert source.count(before) == 1
    sync = (w.root / "scripts/hooks/sync-hooks.sh").read_text()
    return {
        path: source.replace(before, before[:-1] + ', "future_chain_helper.py")'),
        "scripts/hooks/sync-hooks.sh": sync.replace('    "pre_push_gates.sh"\n', '    "pre_push_gates.sh"\n    "future_chain_helper.py"\n'),
        "scripts/hooks/future_chain_helper.py": "# inert future owned helper\n",
    }


@pytest.mark.parametrize("change", ["expand", "reorder", "missing_source"])
def test_candidate_closure_change_is_refused_before_admission(dc, dc_ready, change):
    w = dc_ready
    changes = _expanded_chain_changes(w)
    if change == "reorder":
        changes = {"scripts/hooks/pre_push_chain.py": (w.root / "scripts/hooks/pre_push_chain.py").read_text().replace(
            'HELPERS = ("pre_push_chain.py", "pre_push_gates.sh")',
            'HELPERS = ("pre_push_gates.sh", "pre_push_chain.py")')}
    elif change == "missing_source":
        changes = {"scripts/hooks/pre_push_chain.py": None}
    w.candidate("feat/closure", changes)
    before = w.rev("HEAD")
    manifest = w.manifest_path.read_bytes() if w.manifest_path.exists() else None
    assert w.add(dc, "feat/closure", "--approve-hooks", "owner, in chat") == 1
    assert w.rev("HEAD") == before
    assert (w.manifest_path.read_bytes() if w.manifest_path.exists() else None) == manifest


@pytest.mark.parametrize("operation", ["drop", "rebuild"])
def test_existing_expanded_candidate_drop_refuses_before_any_mutation(dc, dc_ready, monkeypatch, operation):
    w = dc_ready
    w.candidate("feat/closure", _expanded_chain_changes(w))
    # Reproduce a candidate admitted by the previous engine, without weakening
    # its installed ownership or source contracts.
    with monkeypatch.context() as old:
        old.setattr(dc.gate, "admission_failures", lambda *args, **kwargs: [])
        old.setattr(dc.gate, "native_closure_change", lambda *args: None)
        assert w.add(dc, "feat/closure", "--approve-hooks", "owner, in chat") == 0
        assert w.run(dc, "rebuild") == 0
    before = w.rev("HEAD")
    manifest = w.manifest_path.read_bytes()
    hooks = w.root / ".git/hooks"
    record = (hooks / ".genesis-pre-push.json").read_bytes()
    args = ("drop", "feat/closure") if operation == "drop" else ("rebuild",)
    assert w.run(dc, *args) == 1
    assert w.rev("HEAD") == before
    assert w.manifest_path.read_bytes() == manifest
    assert (hooks / ".genesis-pre-push.json").read_bytes() == record
    assert dc.gate.hook_install_failures(dc.Engine(w.root, w.env, w.gh, w.serving)) == []


def test_released_main_expansion_is_supported_but_candidate_contraction_is_not(dc, dc_ready):
    w = dc_ready
    changes = _expanded_chain_changes(w)
    originals = {path: (w.root / path).read_text() if (w.root / path).exists() else None for path in changes}
    w.advance_main(changes)
    w.write_manifest([])
    assert w.run(dc, "rebuild") == 0
    assert dc.gate.hook_install_failures(dc.Engine(w.root, w.env, w.gh, w.serving)) == []
    before = w.rev("HEAD")
    w.candidate("feat/contract", originals)
    assert w.add(dc, "feat/contract", "--approve-hooks", "owner, in chat") == 1
    assert w.rev("HEAD") == before


@pytest.mark.parametrize("collision", [None, "file", "dangling-link", "directory"])
def test_candidate_additive_closure_admits_prior_ownership_before_mutation(dc, dc_ready, collision):
    w = dc_ready
    before = w.rev("HEAD")
    changes = _expanded_chain_changes(w)
    changes["scripts/hooks/pre_push_gates.sh"] = (w.root / "scripts/hooks/pre_push_gates.sh").read_text() + "# target revision\n"
    after = w.candidate("feat/closure", changes)
    hooks = w.root / ".git/hooks"
    record = hooks / ".genesis-pre-push.json"
    prior = record.read_bytes()
    gate = hooks / "pre_push_gates.sh"
    gate_before = gate.read_bytes()
    target = hooks / "future_chain_helper.py"
    if collision == "file":
        target.write_text("unowned helper\n")
    elif collision == "dangling-link":
        target.symlink_to(hooks / "missing")
    elif collision == "directory":
        target.mkdir()
    w.git(w.root, "-c", "core.hooksPath=/dev/null", "switch", "-q", "-C", "live", after)
    repo = dc.Engine(w.root, w.env, w.gh, w.serving)
    failures = dc.plan.restore_moved_hooks(repo, before, after)
    if collision:
        assert failures and "destination already exists" in str(failures)
        assert record.read_bytes() == prior and gate.read_bytes() == gate_before
        if collision == "file":
            assert target.read_text() == "unowned helper\n"
        elif collision == "dangling-link":
            assert target.is_symlink()
        else:
            assert target.is_dir()
        return
    assert failures == []
    assert not target.exists(), "generic restore cannot pre-publish a target-only helper"
    assert dc.gate.hook_install_failures(repo), "prior ownership is not exact target readiness"
    assert dc.plan.sync_git_hooks(repo) is None
    assert dc.gate.hook_install_failures(repo) == []
    state = json.loads(record.read_text())
    assert state["closure"] == [*CHAIN_HELPERS, "future_chain_helper.py"]
    assert target.read_bytes() == (w.root / "scripts/hooks/future_chain_helper.py").read_bytes()


def test_candidate_transition_does_not_repair_unowned_missing_helper(dc, dc_ready):
    w = dc_ready
    before = w.rev("HEAD")
    after = w.candidate("feat/closure", _expanded_chain_changes(w))
    missing = w.root / ".git/hooks/pre_push_gates.sh"
    missing.unlink()
    w.git(w.root, "-c", "core.hooksPath=/dev/null", "switch", "-q", "-C", "live", after)
    repo = dc.Engine(w.root, w.env, w.gh, w.serving)
    assert dc.plan.restore_moved_hooks(repo, before, after)
    assert not missing.exists()
    assert not (w.root / ".git/hooks/future_chain_helper.py").exists()


@pytest.mark.parametrize("collision", [False, True])
def test_legacy_reupgrade_recovers_only_prior_owned_closure(dc, dc_ready, collision):
    w = dc_ready
    before = w.rev("HEAD")
    expanded = w.candidate("feat/expanded-closure", _expanded_chain_changes(w))
    sync = (w.root / "scripts/hooks/sync-hooks.sh").read_text()
    arrays = sync[sync.index("HOOKS_TO_SYNC=("):sync.index("MODIFIED_FOUND=0")]
    for name in CHAIN_HELPERS:
        arrays = arrays.replace(f'    "{name}"\n', "")
    legacy = w.candidate("feat/legacy-closure", {
        "scripts/hooks/sync-hooks.sh": "#!/bin/bash\n" + arrays + "exit 0\n",
        "scripts/hooks/pre-push": (w.root / "scripts/hooks/pre_push_gates.sh").read_text(),
        **{f"scripts/hooks/{name}": None for name in CHAIN_HELPERS},
    })
    repo = dc.Engine(w.root, w.env, w.gh, w.serving)
    w.git(w.root, "-c", "core.hooksPath=/dev/null", "switch", "-q", "-C", "live", legacy)
    assert dc.plan.restore_moved_hooks(repo, before, legacy) == []
    hooks = w.root / ".git/hooks"
    assert not (hooks / "pre_push_chain.py").exists()
    record = hooks / ".genesis-pre-push.json"
    prior = record.read_bytes()
    target = hooks / "future_chain_helper.py"
    if collision:
        target.write_text("unowned future helper\n")
    w.git(w.root, "-c", "core.hooksPath=/dev/null", "switch", "-q", "-C", "live", expanded)
    failures = dc.plan.restore_moved_hooks(repo, legacy, expanded)
    assert record.read_bytes() == prior
    if collision:
        assert failures and "destination already exists" in str(failures)
        assert not (hooks / "pre_push_chain.py").exists()
        assert target.read_text() == "unowned future helper\n"
        return
    assert failures == []
    assert (hooks / "pre_push_chain.py").is_file()
    assert (hooks / "pre_push_gates.sh").is_file()
    assert not target.exists()
    assert dc.plan.sync_git_hooks(repo) is None
    assert dc.gate.hook_install_failures(repo) == []


@pytest.mark.parametrize("history", ["retained_larger", "dangling_record", "compatible", "record_free"])
def test_legacy_reupgrade_checks_retained_record_before_checkout_move(dc, dc_ready, history):
    w = dc_ready
    paths = ["scripts/hooks/sync-hooks.sh", "scripts/hooks/pre-push",
             "scripts/hooks/pre_push_chain.py", "scripts/hooks/pre_push_gates.sh"]
    small = {path: (w.root / path).read_text() for path in paths}
    expanded = {**small, **_expanded_chain_changes(w)}
    w.advance_main(expanded)
    w.write_manifest([])
    assert w.run(dc, "rebuild") == 0
    sync = expanded["scripts/hooks/sync-hooks.sh"]
    arrays = sync[sync.index("HOOKS_TO_SYNC=("):sync.index("MODIFIED_FOUND=0")]
    for name in ("pre_push_chain.py", "pre_push_gates.sh", "future_chain_helper.py"):
        arrays = arrays.replace(f'    "{name}"\n', "")
    w.advance_main({
        "scripts/hooks/sync-hooks.sh": "#!/bin/bash\n" + arrays + "exit 0\n",
        "scripts/hooks/pre-push": small["scripts/hooks/pre_push_gates.sh"],
        "scripts/hooks/pre_push_chain.py": None, "scripts/hooks/pre_push_gates.sh": None,
        "scripts/hooks/future_chain_helper.py": None,
    })
    assert w.run(dc, "rebuild") == 0
    record = w.root / ".git/hooks/.genesis-pre-push.json"
    assert record.exists()
    if history in ("record_free", "dangling_record"):
        record.unlink()
    if history == "dangling_record":
        record.symlink_to("absent-owned-record")
    w.candidate("feat/reupgrade", expanded if history == "compatible" else small)
    assert w.add(dc, "feat/reupgrade", "--approve-hooks", "owner, in chat") == 0
    head = w.rev("HEAD")
    manifest = w.manifest_path.read_bytes()
    prior_record = record.read_bytes() if record.exists() else None
    result = w.run(dc, "rebuild")
    if history in ("retained_larger", "dangling_record"):
        assert result == 1
        assert w.rev("HEAD") == head
        assert w.manifest_path.read_bytes() == manifest
        if history == "dangling_record":
            assert str(record.readlink()) == "absent-owned-record"
        else:
            assert record.read_bytes() == prior_record
    else:
        assert result == 0
        assert w.rev("HEAD") != head
    assert dc.gate.hook_install_failures(dc.Engine(w.root, w.env, w.gh, w.serving)) == []
