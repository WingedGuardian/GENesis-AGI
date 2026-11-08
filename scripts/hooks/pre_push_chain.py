#!/usr/bin/env python3
"""Installation and execution of the opted-in native pre-push chain.

The original stays adjacent, preserving dirname-based resources, cwd and argv.
An original that requires its exact basename is not a supported chain profile;
opt-in must follow qualification of that hook. Installation alone is not proof
of privacy enforcement. The native privacy consumer is a subsequent concern.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

MANIFEST = ".genesis-pre-push.json"
ORIGINAL = ".genesis-pre-push-original"
HELPERS = ("pre_push_chain.py", "pre_push_gates.sh")
INPUT_LIMIT = 1024 * 1024


def _git(repo: Path, *args: str, env: dict[str, str] | None = None) -> str:
    return os.fsdecode(subprocess.check_output(
        ["git", "-C", str(repo), *args], stderr=subprocess.DEVNULL, env=env,
    )).removesuffix("\n")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _regular(path: Path) -> bool:
    return not path.is_symlink() and path.is_file()


def _atomic(path: Path, data: bytes, mode: int) -> None:
    fd, name = tempfile.mkstemp(prefix=path.name + ".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def _save(path: Path, state: dict) -> None:
    _atomic(path, json.dumps(state, sort_keys=True).encode(), 0o600)


def _load(path: Path) -> dict:
    if not _regular(path):
        raise ValueError("invalid ownership record")
    state = json.loads(path.read_bytes())
    keys = {"version", "phase", "repos", "original", "run_original_for_enrolled", "closure", "helpers", "wrapper_sha256"}
    if not isinstance(state, dict) or type(state.get("version")) is not int or state["version"] != 1:
        raise ValueError("unsupported ownership record")
    phase = state.get("phase")
    if phase not in ("prepared", "active", "restoring") or set(state) != keys | ({"previous"} if phase == "prepared" else set()):
        raise ValueError("invalid ownership phase")
    _snapshot(state)
    if (not isinstance(state["repos"], dict) or any(not isinstance(v, str) or not os.path.isabs(v)
            for pair in state["repos"].items() for v in pair)
            or bool(state["repos"]) != (phase != "restoring")
            or type(state["run_original_for_enrolled"]) is not bool):
        raise ValueError("invalid enrollment")
    original = state["original"]
    if original is not None and (not isinstance(original, dict) or set(original) != {"sha256", "mode"}
            or not _hash(original["sha256"]) or type(original["mode"]) is not int or not 0 <= original["mode"] <= 0o7777):
        raise ValueError("invalid original record")
    previous = state.get("previous")
    if previous is not None:
        if not isinstance(previous, dict) or set(previous) != {"closure", "helpers", "wrapper_sha256"}:
            raise ValueError("invalid prior ownership")
        _snapshot(previous)
        if state["closure"][:len(previous["closure"])] != previous["closure"]:
            raise ValueError("unsupported prior closure")
    return state


def _hash(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _snapshot(state: dict) -> None:
    closure = state.get("closure")
    if (not isinstance(closure, list) or closure[:2] != ["pre_push_chain.py", "pre_push_gates.sh"]
            or any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._-]+", name)
                   or name in (".", "..", "pre-push", MANIFEST, ORIGINAL) for name in closure)
            or len(set(closure)) != len(closure) or not isinstance(state.get("helpers"), dict)
            or set(state["helpers"]) != set(closure)
            or not all(_hash(value) for value in state["helpers"].values()) or not _hash(state.get("wrapper_sha256"))):
        raise ValueError("invalid helper ownership population")


def _original_valid(directory: Path, state: dict) -> bool:
    original = state.get("original")
    path = directory / ORIGINAL
    if original is None:
        return not path.exists() and not path.is_symlink()
    return (
        _regular(path) and _digest(path) == original["sha256"]
        and stat.S_IMODE(path.stat().st_mode) == original["mode"]
    )


def _known_hook(source: Path, hook: Path) -> bool:
    digest = _digest(hook)
    if digest == _digest(source / "pre_push_gates.sh"):
        return True
    versions = source.parents[1] / ".genesis-hook-versions"
    return versions.is_file() and f"pre-push:{digest}" in versions.read_text().splitlines()


def install(source: Path, repo: Path, *, opt_in: bool, restore: bool = False) -> None:
    """Called only by the existing main-owned sync tool for this common gitdir."""
    source = source.resolve()
    # Installation resolves named repositories; native run() keeps its push environment.
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_") or key.startswith("GIT_CONFIG")}
    common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir", env=env)
    if common != _git(source.parents[1], "rev-parse", "--path-format=absolute", "--git-common-dir", env=env):
        raise ValueError("target is not a worktree of the source repository")
    hook = Path(_git(repo, "rev-parse", "--path-format=absolute", "--git-path", "hooks/pre-push", env=env))
    directory = hook.parent
    directory.mkdir(parents=True, exist_ok=True)
    lock = os.open(directory / ".genesis-pre-push.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        _install_locked(source, common, hook, opt_in=opt_in, restore=restore)
    finally:
        os.close(lock)


def _validate_installed(source: Path, hook: Path, state: dict, *, restoring: bool = False) -> None:
    """Validate recorded ownership before any transition can publish files."""
    previous = state.get("previous") or {}
    prepared = state["phase"] == "prepared"
    legacy = hook.exists() and _known_hook(source, hook) and b"pre_push_chain.py" not in hook.read_bytes()
    allowed = {state["wrapper_sha256"]}
    if not restoring:
        allowed.add(_digest(source / "pre-push"))
    if legacy:
        allowed.add(_digest(hook))
    if previous:
        allowed.add(previous["wrapper_sha256"])
    if prepared and not previous and state["original"]:
        allowed.add(state["original"]["sha256"])
    if hook.exists():
        if not _regular(hook) or _digest(hook) not in allowed:
            raise ValueError("hook changed after installation; preserved")
    elif not prepared or previous or state["original"]:
        raise ValueError("installed hook removed; preserved")
    original = hook.parent / ORIGINAL
    if original.exists() or original.is_symlink():
        if not _original_valid(hook.parent, state):
            raise ValueError("preserved original changed; preserved")
    elif state["original"] and not (prepared and not previous):
        raise ValueError("preserved original missing")
    for name, digest in state["helpers"].items():
        path = hook.parent / name
        absent = not path.exists() and not path.is_symlink()
        if absent and ((legacy and not restoring) or (prepared and name not in previous.get("helpers", {}))):
            continue
        allowed = {digest}
        if not restoring and _regular(source / name):
            allowed.add(_digest(source / name))
        if prepared:
            allowed.add(previous.get("helpers", {}).get(name))
        if not _regular(path) or _digest(path) not in allowed:
            raise ValueError("installed helper changed; preserved")


def _restore(source: Path, common: str, hook: Path, state: dict) -> None:
    """Keep the restoring record until terminal hook and owned cleanup converge."""
    record, original = hook.parent / MANIFEST, hook.parent / ORIGINAL
    if state["phase"] == "active":
        _validate_installed(source, hook, state, restoring=True)
        if not _original_valid(hook.parent, state):
            raise ValueError("original unavailable for restoration")
        state["repos"].pop(common, None)
        if state["repos"]:
            _save(record, state)
            return
        state["phase"] = "restoring"
        _save(record, state)
    elif state["phase"] != "restoring":
        raise ValueError("no restorable active chain")
    saved = state["original"]
    terminal = (not hook.exists() and not hook.is_symlink()) if saved is None else (
        _regular(hook) and _digest(hook) == saved["sha256"] and stat.S_IMODE(hook.stat().st_mode) == saved["mode"])
    if not terminal:
        if not _regular(hook) or _digest(hook) != state["wrapper_sha256"] or not _original_valid(hook.parent, state):
            raise ValueError("restore target changed; preserved")
        if saved is None:
            hook.unlink()
        else:
            _atomic(hook, original.read_bytes(), saved["mode"])
    # Only restoring permits absent files: earlier cleanup may already have unlinked them.
    for name, digest in state["helpers"].items():
        path = hook.parent / name
        if not path.exists() and not path.is_symlink():
            continue
        if not _regular(path) or _digest(path) != digest:
            raise ValueError("restore helper changed; preserved")
        path.unlink()
    if original.exists() or original.is_symlink():
        if not _original_valid(hook.parent, state):
            raise ValueError("restore original changed; preserved")
        original.unlink()
    record.unlink()


def _prepare(source: Path, common: str, hook: Path, state: dict | None, *, opt_in: bool) -> tuple[dict, dict, bytes]:
    """Admit a complete additive target closure before claiming any new path."""
    payloads = {name: (source / name).read_bytes() for name in HELPERS}
    wrapper = (source / "pre-push").read_bytes()
    target = {"closure": list(HELPERS), "helpers": {name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()},
              "wrapper_sha256": hashlib.sha256(wrapper).hexdigest()}
    _snapshot(target)
    compile(payloads["pre_push_chain.py"], "pre_push_chain.py", "exec")
    if state is not None:
        if state["phase"] == "restoring":
            raise ValueError("finish the recorded restoration first")
        _validate_installed(source, hook, state)
        if common not in state["repos"] and not opt_in:
            raise ValueError("shared hook preserved; explicit repository enrollment required")
        if target["closure"][:len(state["closure"])] != state["closure"]:
            raise ValueError("helper removal or reorder requires a separate transition")
        if state["phase"] == "prepared":
            if any(state[key] != value for key, value in target.items()):
                raise ValueError("finish the recorded installation before changing its target")
            # Retain the original prior closure across interrupted publication retries.
            return state, payloads, wrapper
    prior_names = state["closure"] if state else []
    for name in target["closure"]:
        path = hook.parent / name
        if name not in prior_names and (path.exists() or path.is_symlink()):
            raise ValueError("new helper destination already exists; preserved")
    if state is None:
        original = hook.parent / ORIGINAL
        if original.exists() or original.is_symlink():
            raise ValueError("preservation path already exists")
        known = hook.exists() and _known_hook(source, hook)
        if hook.exists() and (_digest(hook) == target["wrapper_sha256"] or (known and b"pre_push_chain.py" in hook.read_bytes())):
            raise ValueError("chain wrapper has no ownership record; preserved")
        if hook.exists() and not known and not opt_in:
            raise ValueError("custom hook preserved; explicit chain opt-in required")
        state = {"version": 1, "repos": {}, "original": None, "run_original_for_enrolled": not known}
        if hook.exists():
            state["original"] = {"sha256": _digest(hook), "mode": stat.S_IMODE(hook.stat().st_mode)}
        previous = None
    else:
        previous = {key: state[key] for key in target}
    state.update(target, phase="prepared", previous=previous)
    return state, payloads, wrapper


def _install_locked(source: Path, common: str, hook: Path, *, opt_in: bool, restore: bool) -> None:
    directory = hook.parent
    record, original = directory / MANIFEST, directory / ORIGINAL
    if hook.is_symlink() or (hook.exists() and not _regular(hook)):
        raise ValueError("unsupported hook file type")
    state = _load(record) if record.exists() or record.is_symlink() else None
    if restore:
        if state is None:
            raise ValueError("no restorable chain")
        _restore(source, common, hook, state)
        return
    state, payloads, wrapper = _prepare(source, common, hook, state, opt_in=opt_in)
    state["repos"][common] = str(source.parents[1])
    _save(record, state)
    if state["original"] is not None and not original.exists():
        if not hook.exists() or _digest(hook) != state["original"]["sha256"]:
            raise ValueError("original unavailable during interrupted installation")
        _atomic(original, hook.read_bytes(), state["original"]["mode"])
    if not _original_valid(directory, state):
        raise ValueError("original preservation failed")
    for name, data in payloads.items():
        _atomic(directory / name, data, 0o755)
    if any(_digest(directory / name) != digest for name, digest in state["helpers"].items()):
        raise ValueError("helper installation incomplete")
    _atomic(hook, wrapper, 0o755)
    state["phase"] = "active"
    state.pop("previous")
    _save(record, state)


def run(argv: list[str]) -> int:
    directory = Path(__file__).resolve().parent
    state = _load(directory / MANIFEST)
    if state["phase"] != "active" or tuple(state["closure"]) != HELPERS or not _original_valid(directory, state):
        raise ValueError("native hook chain is unqualified")
    if _digest(directory / "pre-push") != state["wrapper_sha256"]:
        raise ValueError("native wrapper changed")
    common = _git(Path.cwd(), "rev-parse", "--path-format=absolute", "--git-common-dir")
    original = directory / ORIGINAL
    if common not in state["repos"]:
        if state["original"] and state["original"]["mode"] & 0o111:
            os.execv(str(original), [str(original), *argv])  # noqa: S606 — preserve unregistered native stdin
        return 0
    # The main checkout recorded by the installer owns runtime dependencies,
    # including a separate-git-dir install. Only explicit development probes
    # choose the invoking worktree's interpreter first.
    roots = [Path(state["repos"][common])]
    if os.environ.get("GENESIS_HOOK_DEV_LOCAL") == "1":
        roots.insert(0, Path(_git(Path.cwd(), "rev-parse", "--show-toplevel")))
    for root in roots:
        python = root / ".venv/bin/python"
        if os.access(python, os.X_OK):
            if os.path.abspath(sys.executable) != str(python):
                os.execv(str(python), [str(python), __file__, "--", *argv])  # noqa: S606 — trusted recorded runtime
            break
    for name, digest in state["helpers"].items():
        if not _regular(directory / name) or _digest(directory / name) != digest:
            raise ValueError("native helper closure changed")
    data = sys.stdin.buffer.read(INPUT_LIMIT + 1)
    if len(data) > INPUT_LIMIT:
        raise ValueError("native hook input exceeds scan limit")
    consumers = []
    if state["run_original_for_enrolled"] and state["original"] and state["original"]["mode"] & 0o111:
        consumers.append(original)
    consumers.append(directory / "pre_push_gates.sh")
    for consumer in consumers:
        code = subprocess.run([str(consumer), *argv], input=data, check=False).returncode
        if code:
            return code if code > 0 else 128 - code
    return 0


def main() -> int:
    try:
        if sys.argv[1:] == ["--list-helpers"]:
            print("\n".join(HELPERS))
            return 0
        if len(sys.argv) > 1 and sys.argv[1] == "--install":
            parser = argparse.ArgumentParser(description=__doc__)
            parser.add_argument("--install", action="store_true")
            parser.add_argument("--source", type=Path, required=True)
            parser.add_argument("--repo", type=Path, required=True)
            parser.add_argument("--opt-in", action="store_true")
            parser.add_argument("--restore", action="store_true")
            args = parser.parse_args()
            install(args.source, args.repo, opt_in=args.opt_in, restore=args.restore)
            return 0
        if len(sys.argv) < 2 or sys.argv[1] != "--":
            raise ValueError("native invocation separator missing")
        return run(sys.argv[2:])
    except (OSError, ValueError, KeyError, TypeError, SyntaxError, subprocess.SubprocessError):
        print("Genesis native pre-push chain could not be qualified; existing user changes are preserved.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
