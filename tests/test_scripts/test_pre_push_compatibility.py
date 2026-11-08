"""Preserved hooks keep native execution and startup-environment semantics."""
import subprocess

import pytest

from tests.test_scripts.test_pre_push_chain import world as world


def custom(world, content):
    repo, env, git, sync = world
    hooks = repo / "custom-hooks"
    hooks.mkdir()
    git("config", "core.hooksPath", str(hooks))
    hook = hooks / "pre-push"
    hook.write_bytes(content)
    hook.chmod(0o751)
    assert sync("--chain-pre-push").returncode == 0
    return hook


def invocation_root(world, enrolled):
    repo, env, git, sync = world
    if enrolled:
        return repo
    other = repo.parent / "unenrolled"
    subprocess.run(["git", "-c", "init.templateDir=", "init", "-q", str(other)], env=env, check=True)
    return other


@pytest.mark.parametrize("enrolled", [False, True])
@pytest.mark.parametrize("status", [0, 7])
def test_executable_without_shebang_keeps_native_shell_fallback(world, enrolled, status):
    repo, env, git, sync = world
    content = b'printf "%s\\n" "$@"\ncat\nexit ' + str(status).encode() + b"\n"
    hook = custom(world, content)
    data = b"(delete) " + b"0" * 40 + b" refs/heads/topic " + b"1" * 40 + b"\n"
    result = subprocess.run([str(hook), "owned-remote", "owned-destination"],
                            cwd=invocation_root(world, enrolled), env=env,
                            input=data, capture_output=True, timeout=10)
    assert result.returncode == status, result.stderr
    assert result.stdout == b"owned-remote\nowned-destination\n" + data
    original = hook.parent / ".genesis-pre-push-original"
    assert original.read_bytes() == content
    assert original.stat().st_mode & 0o777 == 0o751


@pytest.mark.parametrize("enrolled", [False, True])
@pytest.mark.parametrize("venv", [False, True])
@pytest.mark.parametrize("profile", ["unset", "explicit-c", "utf8", "all-c", "coercion-off"])
def test_original_locale_environment_survives_every_interpreter(world, enrolled, venv, profile):
    repo, env, git, sync = world
    hook = custom(world, b'#!/bin/sh\nprintf "%s:%s\\n" "${LC_CTYPE+x}" "${LC_CTYPE-}"\n')
    if venv:
        python = repo / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.symlink_to("/usr/bin/python3")
    for key in ("LC_ALL", "LC_CTYPE", "PYTHONCOERCECLOCALE"):
        env.pop(key, None)
    env["LANG"] = "C"
    if profile == "explicit-c":
        env["LC_CTYPE"] = "C"
    elif profile == "utf8":
        env["LC_CTYPE"] = "C.UTF-8"
    elif profile == "all-c":
        env["LC_ALL"] = "C"
    elif profile == "coercion-off":
        env["PYTHONCOERCECLOCALE"] = "0"
    expected = ("x:" + env["LC_CTYPE"] if "LC_CTYPE" in env else ":") + "\n"
    result = subprocess.run([str(hook), "owned-remote", "owned-destination"],
                            cwd=invocation_root(world, enrolled), env=env,
                            input=b"", capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == expected.encode()


def test_declining_chain_still_installs_unrelated_hooks_and_helpers(world):
    repo, env, git, sync = world
    source = repo / "scripts/hooks"
    target = repo / ".git/hooks"
    target.mkdir(exist_ok=True)
    original = b"#!/bin/sh\nexit 0\n"
    (target / "pre-push").write_bytes(original)
    (target / "pre-push").chmod(0o751)
    (source / "post-commit").write_text("#!/bin/sh\n# owned unrelated hook\n")
    (source / "post-commit").chmod(0o755)
    (source / "db_admission_check.py").write_text("# owned unrelated helper\n")
    result = sync()
    assert result.returncode == 2
    assert (target / "pre-push").read_bytes() == original
    assert (target / "post-commit").read_bytes() == (source / "post-commit").read_bytes()
    assert (target / "db_admission_check.py").read_bytes() == (source / "db_admission_check.py").read_bytes()
    assert not (target / ".genesis-pre-push.json").exists()


@pytest.mark.parametrize("replace", [False, True])
@pytest.mark.parametrize("error", [2, 13])
def test_native_fallback_is_only_for_enoexec(tmp_path, monkeypatch, replace, error):
    from tests.conftest import private_module
    from tests.test_scripts.test_pre_push_chain import SOURCE

    module = private_module("chain_error_boundary", SOURCE / "pre_push_chain.py")
    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise OSError(error, "owned execution error")

    monkeypatch.setattr(module.os, "execve", fail)
    monkeypatch.setattr(module.subprocess, "run", fail)
    monkeypatch.setattr(module, "_git", lambda *a, **kw: pytest.fail("not an ENOEXEC fallback"))
    with pytest.raises(OSError) as caught:
        module._run_hook(tmp_path / "original", ["remote", "destination"], {b"LANG": b"C"},
                         None if replace else b"owned stdin")
    assert caught.value.errno == error and len(calls) == 1


def test_unreadable_startup_environment_refuses_without_reflection(monkeypatch, capsys):
    from pathlib import Path

    from tests.conftest import private_module
    from tests.test_scripts.test_pre_push_chain import SOURCE

    module = private_module("chain_unreadable_environment", SOURCE / "pre_push_chain.py")
    original = Path.read_bytes
    attempted = []

    def read(path):
        if str(path) == "/proc/self/environ":
            attempted.append(path)
            raise PermissionError("owned-private-error-must-not-be-shown")
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    monkeypatch.setattr(module.sys, "argv", ["pre_push_chain.py", "--", "remote", "destination"])
    assert module.main() == 2
    assert attempted, "the initial environment must be read before accepting a native invocation"
    output = capsys.readouterr()
    assert "could not be qualified" in output.err
    assert not output.out and "owned-private" not in output.err


@pytest.mark.parametrize("enrolled", [False, True])
@pytest.mark.parametrize("venv", [False, True])
@pytest.mark.parametrize("lang", ["C", "C.UTF-8"])
def test_explicit_sibling_loading_preserves_safepath_and_locale(world, enrolled, venv, lang):
    repo, env, git, sync = world
    hook = custom(world, b'#!/bin/sh\nprintf "%s:%s:%s\\n" "$PYTHONSAFEPATH" "${LC_CTYPE+x}" "${LC_CTYPE-}"\ncat\n')
    if venv:
        python = repo / ".venv/bin/python"
        python.parent.mkdir(parents=True)
        python.symlink_to("/usr/bin/python3")
    for key in ("LC_ALL", "LC_CTYPE", "PYTHONCOERCECLOCALE"):
        env.pop(key, None)
    env.update(LANG=lang, PYTHONSAFEPATH="1")
    data = b"(delete) " + b"0" * 40 + b" refs/heads/topic " + b"1" * 40 + b"\n"
    result = subprocess.run([str(hook), "owned-remote", "owned-destination"],
                            cwd=invocation_root(world, enrolled), env=env,
                            input=data, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"1::\n" + data
