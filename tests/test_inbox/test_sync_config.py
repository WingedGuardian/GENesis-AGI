"""genesis.inbox.sync_config: the one config reader the sync script and the
staleness check share (#3183)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from genesis.inbox import sync_config
from genesis.inbox.config import load_inbox_config_from_string


@pytest.mark.parametrize(
    "remote",
    ["dropbox:Apps/vault-sync/Inbox", "gdrive:", "my_remote:path with spaces", "a.b-c:x"],
)
def test_valid_remotes(remote):
    assert sync_config.remote_is_valid(remote)


@pytest.mark.parametrize(
    "remote",
    ["", "no-colon", "-dropbox:x", ":path", "drop box:x", "dropbox:a\nb", "dropbox:\x07"],
)
def test_invalid_remotes(remote):
    assert not sync_config.remote_is_valid(remote)


def test_sync_remote_defaults_to_off():
    cfg = load_inbox_config_from_string("inbox_monitor:\n  watch_path: /tmp/inbox\n")
    assert cfg.sync_remote == ""


_real_overrides = sync_config.apply_secret_overrides


@pytest.fixture(autouse=True)
def _no_real_secrets(monkeypatch):
    """main() must not read this checkout's own secrets.env into the test process."""
    monkeypatch.setattr(sync_config, "apply_secret_overrides", lambda: None)


@pytest.mark.parametrize("value, kept", [("false", "False"), ("0", "0")])
def test_a_falsy_sync_remote_stays_set_and_is_refused(value, kept):
    cfg = load_inbox_config_from_string(
        f"inbox_monitor:\n  watch_path: /tmp/inbox\n  sync_remote: {value}\n"
    )
    assert cfg.sync_remote == kept
    assert not sync_config.remote_is_valid(cfg.sync_remote)


def test_secret_overrides_take_only_their_own_keys(monkeypatch, tmp_path):
    monkeypatch.setenv("SECRETS_PATH", str(tmp_path / "named-by-the-resolver.env"))
    monkeypatch.setenv("GENESIS_HOME", str(tmp_path / "loses"))
    # setenv first so the teardown removes what the override writes: delenv
    # records nothing for an absent variable.
    monkeypatch.setenv("GENESIS_INBOX_PATH", "placeholder")
    monkeypatch.delenv("GENESIS_INBOX_PATH")
    monkeypatch.delenv("UNRELATED_SECRET", raising=False)
    (tmp_path / "named-by-the-resolver.env").write_text(
        "GENESIS_HOME=/from/secrets\nGENESIS_INBOX_PATH=/from/secrets\nUNRELATED_SECRET=abc\n"
    )
    _real_overrides()
    assert os.environ["GENESIS_HOME"] == "/from/secrets"  # as for the server
    assert os.environ["GENESIS_INBOX_PATH"] == "/from/secrets"
    assert "UNRELATED_SECRET" not in os.environ


def test_an_empty_secret_override_clears_the_inherited_value(monkeypatch, tmp_path):
    monkeypatch.setenv("SECRETS_PATH", str(tmp_path / "s.env"))
    monkeypatch.setenv("GENESIS_HOME", "/inherited")
    (tmp_path / "s.env").write_text("GENESIS_HOME=\n")
    _real_overrides()
    assert os.environ["GENESIS_HOME"] == ""


def test_a_refused_remote_is_not_printed(monkeypatch, capsys):
    monkeypatch.setattr(sync_config, "load_sync_settings", lambda: ("-tok3n:x", Path("/tmp/i")))
    assert sync_config.main() == 2
    assert "tok3n" not in capsys.readouterr().err


def test_an_empty_inbox_override_is_unset(monkeypatch):
    monkeypatch.setenv("GENESIS_INBOX_PATH", "")
    cfg = load_inbox_config_from_string("inbox_monitor:\n  watch_path: /tmp/inbox\n")
    assert str(cfg.watch_path) == "/tmp/inbox"


def test_sync_remote_is_kept_verbatim_even_when_invalid():
    """An invalid value must stay SET (so the sync refuses it and the alert
    fires), never degrade to "off"."""
    cfg = load_inbox_config_from_string(
        "inbox_monitor:\n  watch_path: /tmp/inbox\n  sync_remote: '  not a remote  '\n"
    )
    assert cfg.sync_remote == "not a remote"


def test_cli_prints_its_keys(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path))  # the target must sit inside home
    monkeypatch.setattr(sync_config, "load_sync_settings", lambda: ("dropbox:v", tmp_path / "in"))
    monkeypatch.setattr(sync_config, "heartbeat_path", lambda: tmp_path / "hb.json")
    assert sync_config.main() == 0
    out = capsys.readouterr().out.splitlines()
    assert out == [
        "remote=dropbox:v",
        f"watch_path={tmp_path / 'in'}",
        f"heartbeat={tmp_path / 'hb.json'}",
        f"genesis_home={sync_config.genesis_home()}",
    ]


def test_cli_off_prints_an_empty_remote(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sync_config, "load_sync_settings", lambda: ("", tmp_path / "in"))
    assert sync_config.main() == 0
    assert "remote=" in capsys.readouterr().out.splitlines()


def test_cli_refuses_an_invalid_remote(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sync_config, "load_sync_settings", lambda: ("-x:y", tmp_path / "in"))
    assert sync_config.main() == 2
    assert "not a valid rclone remote" in capsys.readouterr().err


def test_cli_reports_an_unreadable_config(monkeypatch, capsys):
    def boom():
        raise KeyError("inbox_monitor.watch_path is required")

    monkeypatch.setattr(sync_config, "load_sync_settings", boom)
    assert sync_config.main() == 1
    assert "could not be read" in capsys.readouterr().err


def test_settings_validator_rejects_a_bad_remote():
    from genesis.mcp.health.settings import _validate_inbox_monitor

    assert _validate_inbox_monitor({"sync_remote": "dropbox:ok"}) == []
    assert _validate_inbox_monitor({"sync_remote": ""}) == []
    assert any("sync_remote" in e for e in _validate_inbox_monitor({"sync_remote": "-x:y"}))
    assert any("sync_remote" in e for e in _validate_inbox_monitor({"sync_remote": 5}))


def test_a_malformed_overlay_raises_instead_of_reading_as_off(monkeypatch, tmp_path):
    """The remote usually lives only in the overlay, so a lenient read of a
    broken overlay would silently turn the sync off (review B1)."""
    from genesis import _config_overlay
    from genesis._config_overlay import ConfigOverlayError

    user_dir = tmp_path / "user_config"
    user_dir.mkdir()
    (user_dir / "inbox_monitor.local.yaml").write_text(
        "inbox_monitor:\n  sync_remote: 'dropbox:x'\n  bad: [unclosed\n"
    )
    monkeypatch.setattr(_config_overlay, "_user_config_dir", lambda: user_dir)

    with pytest.raises(ConfigOverlayError):
        sync_config.load_sync_settings()
    assert sync_config.main() == 1


# --- the mirror target and value hygiene (security review) ----------------------


@pytest.fixture
def fake_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / ".genesis").mkdir(parents=True)
    repo = home / "genesis"
    repo.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(sync_config, "genesis_home", lambda: home / ".genesis")
    monkeypatch.setattr(sync_config, "repo_root", lambda: repo)
    return home


@pytest.mark.parametrize("rel", ["inbox", "inbox/sub", "Notes/Vault"])
def test_safe_mirror_targets(fake_home, rel):
    assert sync_config.mirror_target_problem(fake_home / rel) is None


@pytest.mark.parametrize(
    "target",
    ["HOME", "/", "HOME/.genesis", "HOME/.genesis/inbox", "HOME/genesis", "HOME/genesis/inbox"],
)
def test_dangerous_mirror_targets_are_refused(fake_home, target):
    path = Path(target.replace("HOME", str(fake_home)))
    assert sync_config.mirror_target_problem(path) is not None
    assert sync_config.mirror_target_problem(path, require_inside_home=False) is not None


def test_outside_home_is_refused_only_by_the_strict_rule(fake_home, tmp_path):
    outside = tmp_path / "elsewhere"
    assert sync_config.mirror_target_problem(outside) is not None
    assert sync_config.mirror_target_problem(outside, require_inside_home=False) is None


def test_a_symlink_cannot_point_the_target_at_home(fake_home):
    link = fake_home / "inbox-link"
    link.symlink_to(fake_home)
    assert sync_config.mirror_target_problem(link) is not None


def test_cli_refuses_a_dangerous_watch_path(monkeypatch, fake_home, capsys):
    monkeypatch.setattr(sync_config, "load_sync_settings", lambda: ("dropbox:v", fake_home))
    assert sync_config.main() == 2
    assert "cannot be the sync target" in capsys.readouterr().err


def test_cli_allows_any_watch_path_when_the_sync_is_off(monkeypatch, fake_home, capsys):
    monkeypatch.setattr(sync_config, "load_sync_settings", lambda: ("", fake_home))
    assert sync_config.main() == 0


@pytest.mark.parametrize(
    "bad",
    [
        "in\nheartbeat=/etc/x",
        "in" + chr(0x85) + "x",
        "in" + chr(0x2028) + "x",
        "in" + chr(0x202E) + "x",
    ],
)
def test_control_characters_in_watch_path_are_refused(monkeypatch, fake_home, bad):
    monkeypatch.setattr(sync_config, "load_sync_settings", lambda: ("dropbox:v", fake_home / bad))
    assert sync_config.main() == 2


@pytest.mark.parametrize(
    "bad",
    ["dropbox:" + chr(0x2028) + "x", "x:y" + chr(0x85) + "z", "dropbox:a" + chr(0x202E) + "b"],
)
def test_unicode_separators_and_bidi_in_remote_are_refused(bad):
    assert not sync_config.remote_is_valid(bad)


def test_settings_validator_refuses_a_catastrophic_watch_path(fake_home):
    from genesis.mcp.health.settings import _validate_inbox_monitor

    assert _validate_inbox_monitor({"watch_path": str(fake_home / "inbox")}) == []
    assert any("watch_path" in e for e in _validate_inbox_monitor({"watch_path": str(fake_home)}))
    assert any("watch_path" in e for e in _validate_inbox_monitor({"watch_path": "/"}))
    assert any("watch_path" in e for e in _validate_inbox_monitor({"watch_path": ""}))


@pytest.mark.parametrize("rel", [".ssh", ".config/rclone", ".claude", "Documents/.private/inbox"])
def test_hidden_folders_under_home_are_refused(fake_home, rel):
    problem = sync_config.mirror_target_problem(fake_home / rel)
    assert problem and "hidden" in problem
    assert sync_config.mirror_target_problem(fake_home / rel, require_inside_home=False)


@pytest.mark.parametrize("raw", ["inbox", "dropbox:foo", "-foo", "./inbox"])
def test_a_relative_target_is_refused_from_any_directory(fake_home, monkeypatch, raw):
    monkeypatch.chdir(fake_home)
    assert sync_config.mirror_target_problem(Path(raw)) == "must be an absolute path"


def test_cli_hands_the_script_the_resolved_inbox(monkeypatch, tmp_path, capsys):
    """rclone measures --one-file-system from the path it is given."""
    monkeypatch.setenv("HOME", str(tmp_path))
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real)
    monkeypatch.setattr(sync_config, "load_sync_settings", lambda: ("dropbox:v", tmp_path / "link"))
    monkeypatch.setattr(sync_config, "heartbeat_path", lambda: tmp_path / "hb.json")
    assert sync_config.main() == 0
    assert f"watch_path={real.resolve()}" in capsys.readouterr().out.splitlines()


def test_the_settings_validator_does_not_echo_a_refused_remote():
    from genesis.mcp.health.settings import _validate_inbox_monitor

    errors = _validate_inbox_monitor({"inbox_monitor": {"sync_remote": "bad remote tok3n"}})
    assert errors and "tok3n" not in " ".join(errors)
