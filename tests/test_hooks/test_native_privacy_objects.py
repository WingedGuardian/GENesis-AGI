"""Actual Git object populations, without invoking publishing commands."""
from pathlib import Path

import pytest

from tests.test_hooks.test_native_push_privacy import CANARY, _record, _worker
from tests.test_hooks.test_native_push_privacy import native_world as _native_world


@pytest.fixture
def native_world(tmp_path, request):
    return _native_world.__wrapped__(tmp_path, request)


def _tree_commit(world, text=b"safe\n", path=b"sample", mode=b"100644", parent=None):
    repo, env, git, commit, invoke = world
    blob = git("hash-object", "-w", "--stdin", data=text).strip()
    if mode == b"160000":
        blob = commit("submodule target").encode()
    tree = git("mktree", "-z", data=mode + (b" commit " if mode == b"160000" else b" blob ") + blob + b"\t" + path + b"\0").strip()
    return git("commit-tree", tree.decode(), "-m", "safe", *(["-p", parent] if parent else [])).decode().strip()


@pytest.mark.parametrize("path", ["space name", "tab\tname", "line\nname", "back\\slash", "éclair", "\u0085name", "\u2028name"])
def test_native_filename_bytes_are_preserved_for_projection(native_world, path):
    repo, env, git, commit, invoke = native_world
    clean = _tree_commit(native_world, path=path.encode())
    hit = _tree_commit(native_world, path=(path + CANARY).encode())
    assert _worker(native_world, _record(clean))["verdict"] == "CLEAN"
    assert _worker(native_world, _record(hit))["verdict"] == "TIER_A"


@pytest.mark.parametrize("kwargs", [{"text": b"binary\0bytes"}, {"text": b"invalid\xff"},
                                    {"path": b"invalid\xff"}, {"mode": b"160000"}])
def test_unscannable_native_object_is_explicit(native_world, kwargs):
    oid = _tree_commit(native_world, **kwargs)
    result = _worker(native_world, _record(oid))
    assert result["verdict"] == "COULD_NOT_SCAN" and result["reasons"]


@pytest.mark.parametrize("mode", [b"100644", b"100755", b"120000"])
def test_empty_and_mode_only_changes_complete(native_world, mode):
    base = _tree_commit(native_world, text=b"")
    tip = _tree_commit(native_world, text=b"", mode=mode, parent=base)
    assert _worker(native_world, _record(tip, base))["verdict"] == "CLEAN"


def test_merge_resolution_is_compared_to_every_parent(native_world):
    repo, env, git, commit, invoke = native_world
    first = commit("first", text=CANARY + "\n")
    second = commit("second", parent=first)
    tree = git("rev-parse", first + "^{tree}").decode().strip()
    merge = git("commit-tree", tree, "-p", first, "-p", second, "-m", "merge").decode().strip()
    assert _worker(native_world, _record(merge, second))["verdict"] == "TIER_A"


def test_rename_reads_added_content_despite_git_rename_configuration(native_world):
    repo, env, git, commit, invoke = native_world
    base = commit("old", text=CANARY + "\n", filename="before")
    tip = commit("new", text=CANARY + "\n", filename="after", parent=base)
    git("config", "diff.renames", "true")
    assert _worker(native_world, _record(tip, base))["verdict"] == "TIER_A"


def test_replacement_object_cannot_hide_published_content(native_world):
    repo, env, git, commit, invoke = native_world
    original = commit("original", text=CANARY + "\n")
    replacement = commit("replacement")
    git("update-ref", "refs/replace/" + original, replacement)
    assert _worker(native_world, _record(original))["verdict"] == "TIER_A"


def test_nested_tag_messages_and_unsupported_blob_targets(native_world):
    repo, env, git, commit, invoke = native_world
    tip = commit("safe")

    def tag(oid, kind, message):
        raw = f"object {oid}\ntype {kind}\ntag fixture\ntagger Fixture <fixture@example.invalid> 1 +0000\n\n{message}\n".encode()
        return git("hash-object", "-w", "-t", "tag", "--stdin", data=raw).decode().strip()

    nested = tag(tag(tip, "commit", CANARY), "tag", "outer")
    assert _worker(native_world, _record(nested))["verdict"] == "TIER_A"
    blob = git("hash-object", "-w", "--stdin", data=b"safe").decode().strip()
    assert _worker(native_world, _record(tag(blob, "blob", "safe")))["verdict"] == "COULD_NOT_SCAN"


@pytest.mark.parametrize("native_world", ["sha256"], indirect=True)
def test_sha256_native_oid_population(native_world):
    repo, env, git, commit, invoke = native_world
    clean = commit("safe")
    assert len(clean) == 64
    assert _worker(native_world, _record(clean))["verdict"] == "CLEAN"
    assert _worker(native_world, _record(commit(CANARY, parent=clean), clean))["verdict"] == "TIER_A"


def test_scope_fallback_uses_any_origin_push_url_not_fetch(native_world):
    repo, env, git, commit, invoke = native_world
    (Path(env["HOME"]) / ".genesis/config/genesis.yaml").unlink()
    git("remote", "add", "origin", "git@github.com:fixture/fetch-only.git")
    git("config", "--add", "remote.origin.pushurl", "git@github.com:fixture/private.git")
    git("config", "--add", "remote.origin.pushurl", "git@github.com:fixture/public.git")
    data = _record(commit(CANARY))
    assert _worker(native_world, data)["verdict"] == "TIER_A"
    assert _worker(native_world, data, destination="git@github.com:fixture/fetch-only.git")["verdict"] == "CLEAN"


@pytest.mark.parametrize("configuration", ["github: [", "github: []", "github:\n  user: fixture\n", "42"])
def test_invalid_or_partial_public_configuration_is_unknown(native_world, configuration):
    repo, env, git, commit, invoke = native_world
    (Path(env["HOME"]) / ".genesis/config/genesis.yaml").write_text(configuration)
    assert _worker(native_world, _record(commit("safe")))["verdict"] == "COULD_NOT_SCAN"
