#!/usr/bin/env python3
"""Install, keep and check the Claude Code plugins this repo ships (today: genesis-secret-redact).

    cc_plugins.py apply [--repo-root DIR]   make the install match the lever (bootstrap and update.sh run this)
    cc_plugins.py check [--repo-root DIR]   report the state; exit 0 when it matches the lever

The plugins live in this checkout's `plugins/` directory, registered as a Claude Code directory marketplace at
USER scope, so every session on the install loads them: foreground, subagents and dispatched `claude -p`
sessions alike. A directory marketplace loads IN PLACE from that folder (measured on 2.1.299: the plugin cache
is not what runs), so a deploy that updates the checkout updates the plugin at the next session start; nothing
here needs re-running for that.

Lever: `cc_plugins.secret_redact` in `${GENESIS_HOME:-~/.genesis}/config/genesis.yaml`, `on` (default) or `off`.
`off` uninstalls the plugin. Any other value changes nothing and says so. An owner who disables the plugin with
`claude plugin disable` keeps it disabled: `apply` never re-enables a plugin that is installed (measured: a plain
`claude plugin install` WOULD re-enable it, so `apply` installs only when the plugin is absent).

Only the main checkout registers itself: a linked worktree is skipped, since its directory can be archived away
and every session would then load nothing. A checkout that moved is re-pointed: the marketplace is removed and
added again, which uninstalls its plugins (measured), so a plugin that was disabled is disabled again after. The
marketplace is identified by its name, `genesis`: any marketplace of that name not pointing at this checkout (for
example the public repository added as a GitHub marketplace) is replaced, plugins and all.

Exit codes: 0 consistent / applied (an invalid lever value is applied as "change nothing" and reported),
2 cannot run (no `claude`, not the main checkout, unreadable genesis.yaml, a CLI failure),
3 inconsistent (check only; includes a lever value that is neither on nor off).
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

MARKETPLACE = "genesis"
PLUGINS = {"secret_redact": "genesis-secret-redact"}
#: Claude Code's plugin commands are local file operations that finish in seconds (measured); the bound only
#: keeps a wedged CLI from holding bootstrap or update.sh forever.
_CLI_TIMEOUT_S = 300


def _claude(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["claude", "plugin", *args], capture_output=True, text=True, timeout=_CLI_TIMEOUT_S
    )


def _json(*args: str) -> list:
    run = _claude(*args, "--json")
    if run.returncode != 0:
        raise RuntimeError(f"claude plugin {' '.join(args)} --json exited {run.returncode}")
    return json.loads(run.stdout or "[]")


def lever(key: str) -> str:
    """'on', 'off', or 'invalid:<value>' for the plugin's lever; 'on' when the file or key is absent."""
    home = Path(os.environ.get("GENESIS_HOME") or Path.home() / ".genesis")
    path = home / "config" / "genesis.yaml"
    if not path.exists():
        return "on"
    import yaml

    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"{path} is not valid YAML: {exc}") from exc
    section = data.get("cc_plugins") if isinstance(data, dict) else None
    if section is None:
        return "on"
    if not isinstance(
        section, dict
    ):  # e.g. `cc_plugins: off` written as one switch for all plugins
        return f"invalid:cc_plugins={section}"
    value = section.get(key, "on")
    if value is True or str(value).lower() == "on":
        return "on"
    if value is False or str(value).lower() == "off":
        return "off"
    return f"invalid:{value}"


def is_main_checkout(root: Path) -> bool:
    def rev(flag: str) -> str:
        out = subprocess.run(
            ["git", "-C", str(root), "rev-parse", flag], capture_output=True, text=True, check=True
        )
        return str((root / out.stdout.strip()).resolve())

    return rev("--git-dir") == rev("--git-common-dir")


def state(name: str) -> dict:
    """What Claude Code reports: the marketplace path, and whether the plugin is installed and enabled."""
    mkt = next((m for m in _json("marketplace", "list") if m.get("name") == MARKETPLACE), None)
    plugin = next((p for p in _json("list") if p.get("id") == f"{name}@{MARKETPLACE}"), None)
    return {
        "marketplace_path": mkt.get("path") if mkt else None,
        "installed": plugin is not None,
        "enabled": bool(plugin and plugin.get("enabled")),
    }


def _run_or_raise(*args: str) -> None:
    run = _claude(*args)
    if run.returncode != 0:
        raise RuntimeError(
            f"claude plugin {' '.join(args)} failed: {(run.stderr or run.stdout).strip()[-200:]}"
        )


def apply(root: Path, key: str, name: str) -> list[str]:
    """Make the install match the lever; returns what was done, one line each."""
    want = lever(key)
    if want.startswith("invalid:"):
        return [f"{name}: lever cc_plugins.{key}={want[8:]!r} is not on/off; nothing changed"]
    plugin_id = f"{name}@{MARKETPLACE}"
    path = str((root / "plugins").resolve())
    now = state(name)
    done: list[str] = []
    if want == "off":
        if now["installed"]:
            _run_or_raise("uninstall", plugin_id)
            done.append(f"{name}: uninstalled (lever off)")
        return done or [f"{name}: off (not installed)"]
    if now["marketplace_path"] is not None and now["marketplace_path"] != path:
        was_disabled = now["installed"] and not now["enabled"]
        _run_or_raise("marketplace", "remove", MARKETPLACE)
        try:  # the shared name forces remove-then-add; a failure in between leaves the plugin uninstalled
            _run_or_raise("marketplace", "add", path)
            _run_or_raise("install", plugin_id)
        except RuntimeError as exc:
            # Put the previous registration back, so the failure costs the re-point and not the redaction.
            old = now["marketplace_path"]
            try:
                _run_or_raise("marketplace", "add", old)
                _run_or_raise("install", plugin_id)
                if was_disabled:
                    _run_or_raise("disable", plugin_id)
                restored = f"; restored the previous registration at {old}"
            except RuntimeError:
                restored = f"; {name} is now NOT INSTALLED, so sessions are not redacted until the next run succeeds"
            raise RuntimeError(f"{exc}{restored}") from exc
        if was_disabled:
            _run_or_raise("disable", plugin_id)
        return [
            f"{name}: marketplace re-pointed to {path}"
            + (" (kept disabled)" if was_disabled else "")
        ]
    if now["marketplace_path"] is None:
        _run_or_raise("marketplace", "add", path)
        done.append(f"marketplace {MARKETPLACE} added at {path}")
    if not now["installed"]:
        _run_or_raise("install", plugin_id)
        done.append(f"{name}: installed")
    elif not now["enabled"]:
        done.append(f"{name}: installed but disabled (owner's choice; left as is)")
    return done or [f"{name}: up to date"]


def check(root: Path, key: str, name: str) -> tuple[int, list[str]]:
    want = lever(key)
    now = state(name)
    path = str((root / "plugins").resolve())
    folder_ok = (root / "plugins" / name / ".claude-plugin" / "plugin.json").is_file()
    lines = [
        f"{name}: lever={want} installed={now['installed']} enabled={now['enabled']} "
        f"marketplace_path={now['marketplace_path']} expected_path={path} plugin_folder_present={folder_ok}"
    ]
    if want.startswith("invalid:"):
        return 3, [*lines, "inconsistent: the lever is not on/off"]
    if want == "off":
        return (
            (3, [*lines, "inconsistent: installed while the lever is off"])
            if now["installed"]
            else (0, lines)
        )
    problems = []
    if now["marketplace_path"] != path:
        problems.append("the marketplace does not point at this checkout")
    if not folder_ok:
        problems.append("the plugin folder is missing from the checkout")
    if not now["installed"]:
        problems.append("not installed")
    elif not now["enabled"]:
        problems.append("disabled while the lever is on (sessions are not redacted)")
    return (3, [*lines, *(f"inconsistent: {p}" for p in problems)]) if problems else (0, lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("mode", choices=("apply", "check"))
    parser.add_argument("--repo-root", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args(argv)
    root = Path(args.repo_root).resolve()
    if shutil.which("claude") is None:
        print(
            "cc-plugins: claude is not on PATH; Claude Code plugins not installed (re-attempted on the next update)"
        )
        return 2
    try:
        if not is_main_checkout(root):
            print(
                f"cc-plugins: {root} is a linked worktree; plugins are registered only from the main checkout"
            )
            return 2
        status = 0
        for key, name in PLUGINS.items():
            if args.mode == "apply":
                for line in apply(root, key, name):
                    print(f"cc-plugins: {line}")
            else:
                rc, lines = check(root, key, name)
                status = max(status, rc)
                for line in lines:
                    print(f"cc-plugins: {line}")
        return status
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, ImportError) as exc:
        print(f"cc-plugins: could not {args.mode} Claude Code plugins: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
