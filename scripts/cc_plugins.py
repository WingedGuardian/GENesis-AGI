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
and every session would then load nothing. A checkout that moved is re-pointed in place (`marketplace add` over
the same name keeps the install and an owner's disable, measured). The marketplace is identified by its name,
`genesis`: one of that name that an in-place add does not re-point (another kind of source, for example the public
repository added as a GitHub marketplace) is replaced by remove-then-add, which uninstalls its plugin, so an
owner's disable is written down first and applied again after.

Exit codes: 0 consistent / applied (an invalid lever value is applied as "change nothing" and reported),
2 cannot run (no `claude`, not the main checkout, unreadable genesis.yaml, a CLI failure),
3 inconsistent (check only; includes a lever value that is neither on nor off),
4 installed as the lever says, but a session in the last day reported that it could not load the secret values
  (it ran unredacted, or on values loaded earlier), or that the secrets file holds placeholder text as a value
  (check only; the sessions' reports are `${GENESIS_HOME:-~/.genesis}/state/secret-redact/<session>.json`,
  names and counts only).
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
PLUGINS = {
    "secret_redact": "genesis-secret-redact",  # pragma: allowlist secret (a lever name and a plugin name)
}
#: Claude Code's plugin commands are local file operations that finish in seconds (measured); the bound only
#: keeps a wedged CLI from holding bootstrap or update.sh forever.
_CLI_TIMEOUT_S = float(
    os.environ.get("GENESIS_CC_PLUGINS_TIMEOUT_S") or 300
)  # the override is for tests


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
    """What Claude Code reports for the user scope: whether a marketplace of this name exists, the folder it is
    read from (a git or GitHub marketplace has none, measured on 2.1.299: it lists ``source`` and ``repo``),
    what re-adds it, and whether the plugin is installed at user scope and enabled. An install at project or
    local scope does not count: it loads only in that project."""
    mkt = next((m for m in _json("marketplace", "list") if m.get("name") == MARKETPLACE), None)
    plugin = next(
        (
            p
            for p in _json("list")
            if p.get("id") == f"{name}@{MARKETPLACE}" and p.get("scope", "user") == "user"
        ),
        None,
    )
    source = None
    if mkt:
        source = mkt.get("path") or mkt.get("repo") or mkt.get("url")
    return {
        "marketplace": mkt is not None,
        "marketplace_path": mkt.get("path") if mkt and mkt.get("source") == "directory" else None,
        "marketplace_source": source,
        "installed": plugin is not None,
        "enabled": bool(plugin and plugin.get("enabled")),
    }


def _run_or_raise(*args: str) -> None:
    run = _claude(*args)
    if run.returncode != 0:
        raise RuntimeError(
            f"claude plugin {' '.join(args)} failed: {(run.stderr or run.stdout).strip()[-200:]}"
        )


def _keep_disabled_marker(name: str) -> Path:
    """Where the owner's disable is written down while a destructive re-point is in flight."""
    home = Path(os.environ.get("GENESIS_HOME") or Path.home() / ".genesis")
    return home / "state" / "cc_plugins" / f"{name}.keep-disabled"


def _replace_marketplace(name: str, plugin_id: str, path: str, now: dict) -> list[str]:
    """Remove-then-add, for a same-named marketplace the in-place add did not re-point (another kind of source).

    Removing the marketplace uninstalls its plugin and forgets an owner's disable (measured), so that choice is
    written down first and cleared only after a disable succeeds; `apply` honours a pending one before anything
    else. A failure in between restores the previous registration where it can.
    """
    marker = _keep_disabled_marker(name)
    if now["installed"] and not now["enabled"]:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("the owner disabled this plugin; re-disable it after a re-point\n")
    try:
        _run_or_raise("marketplace", "remove", MARKETPLACE)
        _run_or_raise("marketplace", "add", path)
        _run_or_raise("install", plugin_id, "--scope", "user")
    except (RuntimeError, subprocess.SubprocessError) as exc:  # a CLI timeout is a SubprocessError
        old = now["marketplace_source"]
        try:
            _claude("marketplace", "remove", MARKETPLACE)
            if not old:
                raise RuntimeError("the previous registration's source is unknown")
            _run_or_raise("marketplace", "add", old)
            _run_or_raise("install", plugin_id, "--scope", "user")
            restored = f"; restored the previous registration at {old}"
        except (RuntimeError, subprocess.SubprocessError):
            restored = f"; {name} may now be NOT INSTALLED, so sessions may not be redacted until the next run succeeds"
        _honour_keep_disabled(name, plugin_id)
        raise RuntimeError(f"{exc}{restored}") from exc
    kept = _honour_keep_disabled(name, plugin_id)
    return [f"{name}: marketplace replaced, now at {path}" + (" (kept disabled)" if kept else "")]


def _honour_keep_disabled(name: str, plugin_id: str) -> bool:
    """Re-disable the plugin when the owner's disable is written down and the plugin is enabled; True if a
    pending choice is now honoured. The marker stays until a disable succeeds."""
    marker = _keep_disabled_marker(name)
    if not marker.exists():
        return False
    now = state(name)
    if not now["installed"]:
        return False  # installed later by apply, which honours the marker then
    try:
        if now["enabled"]:
            _run_or_raise("disable", plugin_id, "--scope", "user")
    except (RuntimeError, subprocess.SubprocessError):
        return False
    marker.unlink(missing_ok=True)
    return True


def apply(root: Path, key: str, name: str) -> list[str]:
    """Make the install match the lever; returns what was done, one line each."""
    want = lever(key)
    prune_reports()
    if want.startswith("invalid:"):
        return [f"{name}: lever cc_plugins.{key}={want[8:]!r} is not on/off; nothing changed"]
    plugin_id = f"{name}@{MARKETPLACE}"
    path = str((root / "plugins").resolve())
    now = state(name)
    done: list[str] = []
    if want == "off":
        if now["installed"]:
            _run_or_raise("uninstall", plugin_id, "--scope", "user")
            done.append(f"{name}: uninstalled (lever off)")
        return done or [f"{name}: off (not installed)"]
    if now["marketplace"] and now["marketplace_path"] != path:
        # In place first: `marketplace add` over the same name re-points a directory marketplace and keeps the
        # install and an owner's disable (measured on 2.1.299), so no step can lose the owner's choice.
        _run_or_raise("marketplace", "add", path)
        after = state(name)
        if after["marketplace_path"] != path:
            return _replace_marketplace(name, plugin_id, path, now)
        now = after
        done.append(f"{name}: marketplace re-pointed to {path}")
    if not now["marketplace"]:
        _run_or_raise("marketplace", "add", path)
        done.append(f"marketplace {MARKETPLACE} added at {path}")
    if not now["installed"]:
        _run_or_raise("install", plugin_id, "--scope", "user")
        done.append(f"{name}: installed")
        if _honour_keep_disabled(name, plugin_id):
            done.append(
                f"{name}: disabled again (the owner's choice, kept through an earlier re-point)"
            )
        return done
    if _honour_keep_disabled(name, plugin_id):
        done.append(
            f"{name}: disabled again (the owner's choice, kept through an earlier re-point)"
        )
    elif not now["enabled"]:
        done.append(f"{name}: installed but disabled (owner's choice; left as is)")
    return done or [f"{name}: up to date"]


#: Sessions report here (the plugin's mod writes one file per session); `check` reads the last day of them.
STATE_DIR = "state/secret-redact"
#: How bad each `check` exit code is: cannot run (2) > wrongly installed (3) > a session reported trouble (4).
_RANK = {0: 0, 4: 1, 3: 2, 2: 3}
STATE_WINDOW_S = 24 * 3600


def session_reports(now: float | None = None) -> tuple[int, list[str]]:
    """(exit code, lines) from the sessions' own reports: 4 when a recent session could not load the secret
    values (it ran unredacted) or found placeholder text stored as a value in the secrets file, else 0."""
    import time  # noqa: PLC0415

    home = Path(os.environ.get("GENESIS_HOME") or Path.home() / ".genesis")
    now = time.time() if now is None else now
    recent = []
    for path in sorted((home / STATE_DIR).glob("*.json")):
        try:
            if now - path.stat().st_mtime > STATE_WINDOW_S:
                continue
            report = json.loads(path.read_text())
        except (OSError, ValueError):
            report = None
        if not isinstance(report, dict):
            report = {"ok": False, "failure": "unreadable report"}
        recent.append((path.stem, report))
    failed = [(sid, r) for sid, r in recent if r.get("ok") is not True]
    overwritten = sorted(
        {
            str(v)
            for _, r in recent
            if isinstance(r.get("overwritten"), list)
            for v in r["overwritten"]
        }
    )
    lines = [f"session reports (last day): {len(recent)}, failed to load: {len(failed)}"]
    if not recent:
        lines.append(
            "no session reported in the last day: whether sessions redact is unknown, not confirmed"
        )
    for sid, r in failed:
        state = "kept earlier values" if r.get("redacting") else "NOT redacting"
        lines.append(
            f"inconsistent: session {sid[:8]} could not load secret values ({r.get('failure')}); {state}"
        )
    if overwritten:
        lines.append(
            "inconsistent: the secrets file holds placeholder text, not a value, for "
            + ", ".join(overwritten)
            + "; restore those keys from a backup"
        )
    return (4 if failed or overwritten else 0), lines


def prune_reports(now: float | None = None, keep_s: float = 7 * 24 * 3600) -> int:
    """Remove session reports older than a week (one file per session; nothing else removes them)."""
    import time  # noqa: PLC0415

    home = Path(os.environ.get("GENESIS_HOME") or Path.home() / ".genesis")
    now = time.time() if now is None else now
    removed = 0
    folder = home / STATE_DIR
    if folder.is_symlink():  # never delete in a folder this script did not make
        return 0
    for path in folder.glob("*.json"):
        try:
            if not path.is_symlink() and now - path.stat().st_mtime > keep_s:
                path.unlink()
                removed += 1
        except OSError:
            pass
    return removed


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
    if problems:
        return 3, [*lines, *(f"inconsistent: {p}" for p in problems)]
    # Installed as the lever says: whether sessions actually redact is what they report themselves.
    code, reported = session_reports()
    return code, [*lines, *reported]


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
                status = max(status, rc, key=_RANK.get)
                for line in lines:
                    print(f"cc-plugins: {line}")
        return status
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError, ImportError) as exc:
        print(f"cc-plugins: could not {args.mode} Claude Code plugins: {exc}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
