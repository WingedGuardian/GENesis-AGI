"""Tests for genesis.util.systemd — systemctl --user subprocess environment."""

import os

import genesis.util.systemd as systemd_module
from genesis.util.systemd import systemctl_env


class TestSystemctlEnv:
    def test_missing_variables_are_injected(self, monkeypatch):
        # Neither variable is inherited (the systemd-service case): both must be
        # derived from the uid so systemctl --user can reach the session manager.
        monkeypatch.setattr(systemd_module.os, "getuid", lambda: 4242)
        for var in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
            monkeypatch.delenv(var, raising=False)

        env = systemctl_env()

        assert env["XDG_RUNTIME_DIR"] == "/run/user/4242"
        assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/4242/bus"

    def test_set_variables_preserved_unchanged(self, monkeypatch):
        # Already-exported values must survive verbatim — the function is a
        # no-op copy, never a rewrite to the uid-derived defaults.
        monkeypatch.setattr(systemd_module.os, "getuid", lambda: 4242)
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/custom/runtime")
        monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", "unix:path=/custom/sock")

        env = systemctl_env()

        assert env["XDG_RUNTIME_DIR"] == "/custom/runtime"
        assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/custom/sock"

    def test_partially_set_only_missing_one_injected(self, monkeypatch):
        monkeypatch.setattr(systemd_module.os, "getuid", lambda: 4242)
        monkeypatch.setenv("XDG_RUNTIME_DIR", "/custom/runtime")
        monkeypatch.delenv("DBUS_SESSION_BUS_ADDRESS", raising=False)

        env = systemctl_env()

        assert env["XDG_RUNTIME_DIR"] == "/custom/runtime"
        assert env["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/user/4242/bus"

    def test_returns_copy_not_os_environ(self, monkeypatch):
        # Mutating the returned dict must not leak into the process environment
        # and vice versa — callers may tweak the dict before subprocess.run.
        monkeypatch.setattr(systemd_module.os, "getuid", lambda: 4242)
        for var in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
            monkeypatch.delenv(var, raising=False)
        sentinel = "__SYSTEMCTL_ENV_SENTINEL__"

        env = systemctl_env()
        env[sentinel] = "1"

        assert sentinel not in os.environ

    def test_does_not_mutate_process_environment(self, monkeypatch):
        monkeypatch.setattr(systemd_module.os, "getuid", lambda: 4242)
        for var in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"):
            monkeypatch.delenv(var, raising=False)

        before = {k: os.environ.get(k) for k in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")}
        systemctl_env()

        after = {k: os.environ.get(k) for k in ("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS")}
        assert before == after
