"""``python -m genesis.trash put`` and the live-database refusal (#2926 PR 6a).

The suite-wide fixtures keep the trash in tmp and point ``genesis_db_path`` at
``tmp_path / "isolated-genesis.db"``. Always read it through the ``genesis.env``
module: a name imported before the fixture runs is the REAL path.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import genesis.env as env
from genesis.trash import ITEM, TrashRefused, list_entries, trash
from genesis.trash.__main__ import EXIT_REFUSED, EXIT_USAGE, main


def _touch(p: Path, text: str = "x") -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


def test_put_trashes_every_path_and_prints_each_id(tmp_path, capsys):
    a = _touch(tmp_path / "a.txt", "aa")
    d = tmp_path / "dir"
    _touch(d / "b.txt")
    assert main(["put", str(a), str(d), "--reason", "tidy up"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert not a.exists() and not d.exists()
    entries = list_entries()
    assert {e.tombstone.caller for e in entries} == {"cli"}
    assert {e.tombstone.reason for e in entries} == {"tidy up"}
    assert len(out) == 2 and all(e.path.name in "\n".join(out) for e in entries)


def test_one_refusal_does_not_stop_the_rest(tmp_path, capsys):
    a = _touch(tmp_path / "a.txt")
    missing = tmp_path / "missing.txt"
    assert main(["put", str(missing), str(a), "--reason", "r"]) == EXIT_REFUSED
    assert not a.exists()  # trashed despite the earlier refusal
    assert "does not exist" in capsys.readouterr().err  # unlike rm -f


def test_put_needs_a_reason(tmp_path):
    a = _touch(tmp_path / "a.txt")
    with pytest.raises(SystemExit) as exc:
        main(["put", str(a)])
    assert exc.value.code == EXIT_USAGE
    assert a.exists()


@pytest.mark.parametrize("which", ["file", "wal", "shm", "dir"])
def test_the_live_database_is_never_trashed(which):
    db = env.genesis_db_path()
    _touch(db, "db")
    target = {
        "file": db,
        "wal": _touch(db.with_name(db.name + "-wal")),
        "shm": _touch(db.with_name(db.name + "-shm")),
        "dir": None,
    }[which]
    if which == "dir":
        # The isolated database sits directly in tmp_path, which the trash
        # already refuses as a parent of itself; a fresh holder isolates the rule.
        holder = db.parent / "holder"
        holder.mkdir()
        inner = _touch(holder / db.name)
        mp = pytest.MonkeyPatch()
        mp.setattr(env, "genesis_db_path", lambda: inner)
        try:
            with pytest.raises(TrashRefused, match="live Genesis database"):
                trash(holder, reason="r", caller="c")
        finally:
            mp.undo()
        assert inner.exists()
        return
    with pytest.raises(TrashRefused, match="live Genesis database"):
        trash(target, reason="r", caller="c")
    assert target.exists()


def test_an_old_database_copy_beside_it_can_be_trashed():
    db = env.genesis_db_path()
    _touch(db, "db")
    old = _touch(db.with_name(db.name + ".pre-restore.1"))
    stone = trash(old, reason="r", caller="c")
    assert not old.exists() and db.exists()
    assert stone.name == old.name
    assert (Path(stone.root) / stone.entry_id / ITEM).exists()


def test_the_database_path_is_isolated_in_tests(tmp_path):
    # Guard-the-guard: these tests must never touch a real database file.
    assert env.genesis_db_path().is_relative_to(tmp_path)


def test_a_symlink_at_the_configured_path_is_refused(tmp_path, monkeypatch):
    real = _touch(tmp_path / "real" / "genesis.db", "db")
    link = tmp_path / "linkdir" / "genesis.db"
    link.parent.mkdir()
    link.symlink_to(real)
    monkeypatch.setattr(env, "genesis_db_path", lambda: link)
    with pytest.raises(TrashRefused, match="live Genesis database"):
        trash(link, reason="r", caller="c")
    assert link.is_symlink()


def test_an_empty_reason_is_a_usage_error(tmp_path):
    a = _touch(tmp_path / "a.txt")
    with pytest.raises(SystemExit) as exc:
        main(["put", str(a), "--reason", "  "])
    assert exc.value.code == EXIT_USAGE
    assert a.exists()


# The server reads secrets.env (systemd EnvironmentFile, then load_dotenv with
# override) and runs from the repository root; this CLI sees neither, so every
# reading of GENESIS_DB_PATH is protected.


@pytest.fixture
def repo(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(env, "repo_root", lambda: root)
    monkeypatch.setattr(env, "secrets_path", lambda: root / "secrets.env")
    monkeypatch.delenv("GENESIS_DB_PATH", raising=False)
    return root


@pytest.mark.parametrize("quote", ["", '"', "'"])
def test_a_database_set_only_in_secrets_env_is_refused(tmp_path, repo, quote):
    live = _touch(tmp_path / "elsewhere" / "live.db", "db")
    (repo / "secrets.env").write_text(f"API_KEY=x\nexport GENESIS_DB_PATH={quote}{live}{quote}\n")
    with pytest.raises(TrashRefused, match="live Genesis database"):
        trash(live, reason="r", caller="c")
    assert live.read_text() == "db"


def test_an_inline_comment_after_the_value_is_not_part_of_the_path(tmp_path, repo):
    live = _touch(tmp_path / "elsewhere" / "live.db", "db")
    (repo / "secrets.env").write_text(f"GENESIS_DB_PATH={live} # moved off the root disk\n")
    with pytest.raises(TrashRefused, match="live Genesis database"):
        trash(live, reason="r", caller="c")
    assert live.exists()


def test_a_relative_value_is_protected_from_the_repository_root(tmp_path, repo, monkeypatch):
    live = _touch(repo / "data" / "other.db", "db")
    (repo / "secrets.env").write_text("GENESIS_DB_PATH=data/other.db\n")
    monkeypatch.chdir(tmp_path)  # the caller is not where the server runs
    with pytest.raises(TrashRefused, match="live Genesis database"):
        trash(live, reason="r", caller="c")
    assert live.exists()


def test_a_relative_environment_value_is_protected_from_the_repository_root(
    tmp_path, repo, monkeypatch
):
    live = _touch(repo / "data" / "env.db", "db")
    monkeypatch.setenv("GENESIS_DB_PATH", "data/env.db")
    monkeypatch.chdir(tmp_path)
    with pytest.raises(TrashRefused, match="live Genesis database"):
        trash(live, reason="r", caller="c")
    assert live.exists()


def test_the_default_location_stays_protected_when_another_is_configured(repo):
    default = _touch(repo / "data" / "genesis.db", "db")
    with pytest.raises(TrashRefused, match="live Genesis database"):
        trash(default, reason="r", caller="c")
    assert default.exists()


def test_an_unresolvable_database_path_refuses_instead_of_crashing(tmp_path, repo, monkeypatch):
    monkeypatch.setenv("GENESIS_DB_PATH", "~no-such-user-genesis-test/db")
    item = _touch(tmp_path / "plain.txt")
    with pytest.raises(TrashRefused, match="live Genesis database"):
        trash(item, reason="r", caller="c")
    assert item.exists()


def test_an_unrelated_file_is_still_trashed_with_a_secrets_value(tmp_path, repo):
    (repo / "secrets.env").write_text(f"GENESIS_DB_PATH={tmp_path / 'live.db'}\n")
    item = _touch(tmp_path / "notes.txt")
    trash(item, reason="r", caller="c")
    assert not item.exists()
