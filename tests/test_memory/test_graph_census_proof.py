"""The census's proof rules: what can be PROVEN about another process.

The hourly census may start the FalkorDB cutover clock only on evidence. These
rules say, for one live process, whether its code provably came from the main
checkout, what its kill switch was at start, and which database it uses, and
every rule answers "unknown" rather than guessing. The fixture builds a main
tree shaped like a live install: a venv whose python is a symlink to the system
interpreter, the three ``.pth`` files that install carries, and a system
``sitecustomize`` that imports a module (as Debian's apport hook does).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from genesis.memory import graph_census_proof as proof

DISTUTILS = (
    "import os; var = 'SETUPTOOLS_USE_DISTUTILS'; enabled = os.environ.get(var, 'local')"
    " == 'local'; enabled and __import__('_distutils_hack').add_shim(); \n"
)


@pytest.fixture
def tree(tmp_path) -> dict:
    sys_bin = tmp_path / "sys" / "bin"
    sys_bin.mkdir(parents=True)
    interp = sys_bin / "python3.12"
    interp.write_text("")
    stdlib = tmp_path / "sys" / "lib" / "python3.12"
    stdlib.mkdir(parents=True)
    (stdlib / "sitecustomize.py").write_text(
        "try:\n    import apport_python_hook\nexcept ImportError:\n    pass\n"
    )
    main = tmp_path / "main"
    (main / "src" / "genesis").mkdir(parents=True)
    (main / "src" / "genesis" / "__init__.py").write_text("")
    (main / "scripts").mkdir()
    (main / "scripts" / "genesis_mcp_server.py").write_text("")
    (main / "scripts" / "genesis_hook_helper.py").write_text("")  # a prefix, not a shadow
    venv = main / ".venv"
    (venv / "bin").mkdir(parents=True)
    (venv / "bin" / "python3").symlink_to(interp)
    (venv / "bin" / "python").symlink_to("python3")
    (venv / "pyvenv.cfg").write_text(
        f"home = {sys_bin}\ninclude-system-site-packages = false\n"
        f"version = 3.12.3\nexecutable = {interp}\n"
    )
    site = venv / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (site / "distutils-precedence.pth").write_text(DISTUTILS)
    (site / "__editable__.genesis_v3-3.0.0b18.pth").write_text(f"{main / 'src'}\n")
    (site / "__editable__.cloudsleuth-0.1.0.pth").write_text(
        "import __editable___cloudsleuth_0_1_0_finder; "
        "__editable___cloudsleuth_0_1_0_finder.install()"
    )
    (site / "__editable___cloudsleuth_0_1_0_finder.py").write_text(
        "MAPPING: dict[str, str] = {'anomaly_detector': '/elsewhere/anomaly_detector'}\n"
        "NAMESPACES: dict[str, list[str]] = {}\n"
    )
    return {"main": main, "venv": venv, "site": site, "interp": interp, "tmp": tmp_path}


def _proc(tree, *, argv=None, env=None, start=None, exe=None, ns="mnt:[1]") -> proof.ProcView:
    main = tree["main"]
    return proof.ProcView(
        pid=4242,
        argv=tuple(
            argv
            or [
                str(main / ".venv" / "bin" / "python"),
                str(main / "scripts" / "genesis_mcp_server.py"),
                "--server",
                "memory",
            ]
        ),
        env=dict(env or {}),
        start=time.time() + 60 if start is None else start,
        exe=str(tree["interp"]) if exe is None else exe,
        mnt_ns=ns,
    )


def _source(tree, proc) -> list[str]:
    return proof.interpreter_source(proc, main_root=tree["main"], own_mnt_ns="mnt:[1]")


# ── import source ─────────────────────────────────────────────────────────


def test_a_server_on_the_main_venv_is_proven_main(tree):
    assert _source(tree, _proc(tree)) == []


def test_another_venv_with_the_same_real_interpreter_is_not_main(tree):
    """B1: every venv's python realpaths to the one system binary, so the venv
    is identified by its bin directory, never by the binary."""
    other = tree["tmp"] / "other" / ".venv" / "bin"
    other.mkdir(parents=True)
    (other / "python").symlink_to(tree["interp"])
    reasons = _source(tree, _proc(tree, argv=[str(other / "python"), "x.py"]))
    assert reasons == ["not_main_venv"]


def test_a_bare_or_relative_argv0_is_unknown(tree):
    for argv0 in ("python", ".venv/bin/python"):
        assert _source(tree, _proc(tree, argv=[argv0, "/x.py"])) == ["argv0_not_absolute"]


def test_another_mount_namespace_is_unknown(tree):
    assert _source(tree, _proc(tree, ns="mnt:[2]")) == ["other_mount_ns"]


def test_an_exe_that_is_not_the_venvs_interpreter_is_unknown(tree):
    reasons = _source(tree, _proc(tree, exe="/usr/bin/some-other-python"))
    assert "exe_mismatch" in reasons


def test_system_site_packages_make_it_unknown(tree):
    cfg = tree["venv"] / "pyvenv.cfg"
    cfg.write_text(cfg.read_text().replace("= false", "= true"))
    assert "venv_cfg_unproven" in _source(tree, _proc(tree))


def test_an_unknown_pth_is_unknown_and_named(tree):
    (tree["site"] / "zz-extra.pth").write_text("/somewhere/else\n")
    assert _source(tree, _proc(tree)) == ["unknown_pth:zz-extra.pth"]


def test_a_hidden_pth_is_skipped_as_site_does(tree):
    (tree["site"] / ".hidden.pth").write_text("/somewhere/else\n")
    assert _source(tree, _proc(tree)) == []


def test_an_editable_install_pointing_at_a_worktree_is_unknown(tree):
    (tree["site"] / "__editable__.genesis_v3-3.0.0b18.pth").write_text("/wt/src\n")
    assert _source(tree, _proc(tree)) == ["unknown_pth:__editable__.genesis_v3-3.0.0b18.pth"]


def test_a_finder_that_maps_genesis_elsewhere_is_unknown(tree):
    finder = tree["site"] / "__editable___cloudsleuth_0_1_0_finder.py"
    finder.write_text("MAPPING = {'genesis': '/wt/src/genesis'}\n")
    assert _source(tree, _proc(tree)) == ["unknown_pth:__editable__.cloudsleuth-0.1.0.pth"]


def test_a_finder_mode_genesis_install_of_main_is_accepted(tree):
    site = tree["site"]
    (site / "__editable__.genesis_v3-3.0.0b18.pth").write_text(
        "import __editable___genesis_v3_finder; __editable___genesis_v3_finder.install()"
    )
    (site / "__editable___genesis_v3_finder.py").write_text(
        f"MAPPING = {{'genesis': {str(tree['main'] / 'src' / 'genesis')!r}}}\n"
    )
    assert _source(tree, _proc(tree)) == []


def test_a_customize_module_in_site_packages_is_unknown(tree):
    (tree["site"] / "sitecustomize.py").write_text("")
    assert _source(tree, _proc(tree)) == ["customize_module"]


@pytest.mark.parametrize("var", ["PYTHONHOME", "PYTHONPLATLIBDIR"])
def test_a_relocated_interpreter_is_unknown(tree, var):
    assert _source(tree, _proc(tree, env={var: "/x"})) == ["python_home_set"]


def test_a_pythonpath_that_cannot_supply_anything_passes(tree):
    route = tree["tmp"] / "route"
    route.mkdir()
    (route / "genesis_sandbox_route.py").write_text("")  # a prefix only
    (route / "genesis_sandbox_route.py.pre-strip-1").write_text("")
    # Both dirs carry a __pycache__: never an import target, never a shadow (the
    # live test-sandbox route dir looks exactly like this).
    (route / "__pycache__").mkdir()
    (tree["site"] / "__pycache__").mkdir()
    assert _source(tree, _proc(tree, env={"PYTHONPATH": str(route)})) == []


@pytest.mark.parametrize(
    "entry",
    [
        "genesis/",
        "genesis.py",
        "genesis.pyc",
        "genesis.cpython-312-x86_64-linux-gnu.so",
        "sitecustomize.py",
        "usercustomize.py",
        "_distutils_hack/",
        "__editable___cloudsleuth_0_1_0_finder.py",
        "apport_python_hook.py",
    ],
)
def test_a_pythonpath_entry_that_could_inject_code_is_unknown(tree, entry):
    """Every name the startup imports by name is banned, including what the .pth
    lines and the system sitecustomize import: PYTHONPATH precedes site-packages."""
    route = tree["tmp"] / "route"
    route.mkdir()
    target = route / entry.rstrip("/")
    if entry.endswith("/"):
        target.mkdir()
    else:
        target.write_text("")
    assert _source(tree, _proc(tree, env={"PYTHONPATH": str(route)})) == ["pythonpath_unproven"]


@pytest.mark.parametrize("value", ["", "relative/dir", "/a:", "/does/not/exist.zip"])
def test_an_empty_relative_or_non_directory_pythonpath_is_unknown(tree, value):
    assert _source(tree, _proc(tree, env={"PYTHONPATH": value or ":"})) == ["pythonpath_unproven"]


def test_a_pythonpath_dir_changed_after_start_is_unknown(tree):
    route = tree["tmp"] / "route"
    route.mkdir()
    proc = _proc(tree, env={"PYTHONPATH": str(route)}, start=time.time() - 60)
    assert _source(tree, proc) == ["pythonpath_unproven"]


def test_a_shadowing_genesis_beside_the_script_is_unknown(tree):
    (tree["main"] / "scripts" / "genesis.py").write_text("")
    assert _source(tree, _proc(tree)) == ["script_dir_shadow"]


def test_a_relative_script_is_unknown(tree):
    argv = [str(tree["main"] / ".venv" / "bin" / "python"), "scripts/genesis_mcp_server.py"]
    assert _source(tree, _proc(tree, argv=argv)) == ["script_not_absolute"]


def test_an_unparseable_system_sitecustomize_is_unknown(tree):
    (tree["tmp"] / "sys" / "lib" / "python3.12" / "sitecustomize.py").write_text("def (:\n")
    assert _source(tree, _proc(tree)) == ["customize_unproven"]


def test_a_reason_never_carries_an_environment_value(tree):
    route = tree["tmp"] / "secret-route-name"
    route.mkdir()
    (route / "genesis.py").write_text("")
    reasons = _source(tree, _proc(tree, env={"PYTHONPATH": str(route)}))
    assert all("secret-route-name" not in r for r in reasons)


# ── secrets file and kill switch ──────────────────────────────────────────


def test_the_secrets_file_is_chosen_the_way_genesis_env_chooses_it(tree):
    main = tree["main"]
    assert proof.secrets_file({}, main_root=main) == main / "secrets.env"
    assert proof.secrets_file({"GENESIS_REPO_ROOT": "/r"}, main_root=main) == Path("/r/secrets.env")
    env = {"GENESIS_REPO_ROOT": "/r", "SECRETS_PATH": "/s/x.env"}
    assert proof.secrets_file(env, main_root=main) == Path("/s/x.env")
    env = {"SECRETS_PATH": "~/s.env", "HOME": "/home/other"}
    assert proof.secrets_file(env, main_root=main) == Path("/home/other/s.env")
    assert proof.secrets_file({"SECRETS_PATH": "~/s.env"}, main_root=main) is None
    assert proof.secrets_file({"SECRETS_PATH": "rel.env"}, main_root=main) is None


def test_a_secrets_file_unchanged_since_start_is_read(tmp_path):
    f = tmp_path / "secrets.env"
    f.write_text("GENESIS_GRAPH_TELEMETRY_DISABLED=1\nOTHER=x\n")
    assert proof.secrets_values(f, time.time() + 60) == {
        "GENESIS_GRAPH_TELEMETRY_DISABLED": "1",
        "OTHER": "x",
    }


def test_a_secrets_file_changed_after_start_is_unknown(tmp_path):
    f = tmp_path / "secrets.env"
    f.write_text("OTHER=x\n")
    assert proof.secrets_values(f, time.time() - 60) is None


def test_a_missing_secrets_file_is_unknown_never_enabled(tmp_path):
    """The binding rule: a missing file does not prove what was loaded at start."""
    assert proof.secrets_values(tmp_path / "absent.env", time.time() + 60) is None


def test_a_secrets_symlink_swapped_after_start_is_unknown(tmp_path):
    real = tmp_path / "real.env"
    real.write_text("OTHER=x\n")
    start = time.time() + 1
    link = tmp_path / "secrets.env"
    # The target predates the start; the LINK is made after it.
    os.utime(real, (start - 100, start - 100))
    time.sleep(1.1)
    link.symlink_to(real)
    assert proof.secrets_values(link, start) is None


def test_a_secrets_path_of_none_is_unknown():
    assert proof.secrets_values(None, time.time()) is None


@pytest.mark.parametrize(
    ("env", "file_vals", "off"),
    [
        # An unprovable file is unknown even with a startup value: the router's
        # override=True reload of that file can replace it.
        ({"GENESIS_GRAPH_TELEMETRY_DISABLED": "1"}, None, None),
        ({}, None, None),
        # The file carries the key: startup and runtime agree only if it matches.
        ({"GENESIS_GRAPH_TELEMETRY_DISABLED": "1"}, {"GENESIS_GRAPH_TELEMETRY_DISABLED": "1"}, True),
        ({"GENESIS_GRAPH_TELEMETRY_DISABLED": "0"}, {"GENESIS_GRAPH_TELEMETRY_DISABLED": "1"}, None),
        ({"GENESIS_GRAPH_TELEMETRY_DISABLED": ""}, {"GENESIS_GRAPH_TELEMETRY_DISABLED": "1"}, None),
        ({}, {"GENESIS_GRAPH_TELEMETRY_DISABLED": "1"}, True),
        ({}, {"GENESIS_GRAPH_TELEMETRY_DISABLED": "0"}, False),
        # The file lacks the key: the startup value decides.
        ({"GENESIS_GRAPH_TELEMETRY_DISABLED": "1"}, {}, True),
        ({"GENESIS_GRAPH_TELEMETRY_DISABLED": "0"}, {}, False),
        ({}, {}, False),
        # A value only interpolation could resolve is unknown from here.
        ({}, {"GENESIS_GRAPH_TELEMETRY_DISABLED": "${OTHER}"}, None),
    ],
)
def test_the_switch_is_what_the_server_runs_with(env, file_vals, off):
    assert proof.switch_off(env, file_vals) is off


# ── database scope ────────────────────────────────────────────────────────


def test_the_primary_db_is_fixed_before_any_secrets_load(tree):
    """_DEFAULT_DB is computed when the server script is imported, so the file
    never moves the primary; its value becomes a second database after the
    router's override reload."""
    main = tree["main"]
    default = main / "data" / "genesis.db"
    assert proof.db_paths({}, {}, main_root=main) == [default]
    assert proof.db_paths({}, None, main_root=main) == [default, None]
    env = {"GENESIS_DB_PATH": "/x.db"}
    assert proof.db_paths(env, {}, main_root=main) == [Path("/x.db")]
    assert proof.db_paths({}, {"GENESIS_DB_PATH": "/f.db"}, main_root=main) == [
        default,
        Path("/f.db"),
    ]
    assert proof.db_paths({"GENESIS_DB_PATH": ""}, {}, main_root=main) == [default]
    assert proof.db_paths({"GENESIS_DB_PATH": "rel.db"}, {}, main_root=main) == [None]
    assert proof.db_paths({"GENESIS_REPO_ROOT": "/r"}, {}, main_root=main) == [
        Path("/r/data/genesis.db")
    ]
    assert proof.db_paths({}, {"GENESIS_DB_PATH": "${X}/f.db"}, main_root=main) == [
        default,
        None,
    ]


def test_a_hardlink_to_the_live_db_is_not_foreign(tmp_path):
    """G7: compared by file identity, not path."""
    live = tmp_path / "genesis.db"
    live.write_text("")
    alias = tmp_path / "alias.db"
    os.link(live, alias)
    foreign, ident = proof.db_scope([alias], live, start=time.time() + 60, open_files=set())
    assert foreign is False
    st = live.stat()
    assert ident == [st.st_dev, st.st_ino]


def test_foreign_only_when_every_database_it_may_use_is_foreign(tmp_path):
    live = tmp_path / "genesis.db"
    live.write_text("")
    bench = tmp_path / "bench.db"
    bench.write_text("")
    other = tmp_path / "other.db"
    other.write_text("")
    assert proof.db_scope([bench], live, start=time.time() + 60, open_files=set())[0] is True
    assert proof.db_scope([bench, other], live, start=time.time() + 60, open_files=set())[0] is True
    # The file names the live DB as the second: some writes may land there.
    assert proof.db_scope([bench, live], live, start=time.time() + 60, open_files=set())[0] is False
    # The second is unknown: never foreign.
    assert proof.db_scope([bench, None], live, start=time.time() + 60, open_files=set())[0] is False


@pytest.mark.parametrize("paths", [[None], [Path("/does/not/exist.db")], []])
def test_an_unknown_or_missing_db_is_never_foreign(tmp_path, paths):
    live = tmp_path / "genesis.db"
    live.write_text("")
    assert proof.db_scope(paths, live, start=time.time() + 60, open_files=set())[0] is False


def test_a_backdated_modification_time_does_not_prove_the_file(tmp_path):
    """ctime, not mtime: `touch -d` or `cp -p` can backdate mtime; ctime still
    says when the file was last changed."""
    f = tmp_path / "secrets.env"
    f.write_text("GENESIS_GRAPH_TELEMETRY_DISABLED=0\n")
    os.utime(f, (1_000_000.0, 1_000_000.0))
    assert proof.secrets_values(f, time.time() - 60) is None


def test_a_customize_module_beside_the_script_is_a_shadow(tree):
    """sys.path[0] precedes everything, so site would import it from there."""
    (tree["main"] / "scripts" / "sitecustomize.py").write_text("")
    assert _source(tree, _proc(tree)) == ["script_dir_shadow"]


def test_a_genesis_package_in_site_packages_shadows_main(tree):
    """BLOCKER (deep review): a path-line .pth puts main src AFTER site-packages."""
    (tree["site"] / "genesis").mkdir()
    assert _source(tree, _proc(tree)) == ["site_packages_shadow"]


def test_the_dist_info_of_the_editable_install_is_not_a_shadow(tree):
    (tree["site"] / "genesis_v3-3.0.0b18.dist-info").mkdir()
    assert _source(tree, _proc(tree)) == []


def test_another_trees_entry_script_is_not_main(tree):
    """The script's own loader and startup decide the switch and DB."""
    wt = tree["tmp"] / "wt" / "scripts"
    wt.mkdir(parents=True)
    (wt / "genesis_mcp_server.py").write_text("")
    argv = [str(tree["main"] / ".venv" / "bin" / "python"), str(wt / "genesis_mcp_server.py")]
    reasons = proof.interpreter_source(
        _proc(tree, argv=argv), main_root=tree["main"], own_mnt_ns="mnt:[1]",
        script=tree["main"] / "scripts" / "genesis_mcp_server.py",
    )
    assert reasons == ["script_not_main"]


def test_a_relative_pth_line_resolves_against_the_site_dir(tree):
    """site.addpackage joins a path line with the site directory."""
    site = tree["site"]
    rel = os.path.relpath(tree["main"] / "src", site)
    (site / "__editable__.genesis_v3-3.0.0b18.pth").write_text(f"{rel}\n")
    assert _source(tree, _proc(tree)) == []


def test_a_pth_line_with_leading_space_is_a_path_not_a_comment(tree):
    """Only a line STARTING with '#' is a comment to site; '  #x' is a path."""
    (tree["site"] / "zz.pth").write_text("  #not-a-comment\n")
    assert _source(tree, _proc(tree)) == ["unknown_pth:zz.pth"]


def test_a_pythonpath_symlink_retargeted_after_start_is_unknown(tree):
    real = tree["tmp"] / "route-real"
    real.mkdir()
    start = time.time() + 1
    time.sleep(1.1)
    link = tree["tmp"] / "route"
    link.symlink_to(real)
    proc = _proc(tree, env={"PYTHONPATH": str(link)}, start=start)
    assert _source(tree, proc) == ["pythonpath_unproven"]


# ── the rule against a REAL interpreter ───────────────────────────────────


def _real_tree(tmp_path) -> dict:
    """A scratch venv of the running interpreter with a main tree on its path,
    exactly as a live install's editable install lays it out."""
    import subprocess
    import sys

    main = tmp_path / "main"
    (main / "src" / "genesis").mkdir(parents=True)
    (main / "src" / "genesis" / "__init__.py").write_text("")
    (main / "scripts").mkdir()
    # Not named genesis_mcp_server.py: the live census scans /proc for that name,
    # and a server-shaped test process would read as an unclassified server.
    probe = main / "scripts" / "probe_entry.py"
    probe.write_text("import genesis, sys\nsys.stdout.write(genesis.__file__)\n")
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(main / ".venv")], check=True
    )
    [site] = (main / ".venv" / "lib").glob("python*/site-packages")
    (site / "__editable__.genesis_v3-0.pth").write_text(f"{main / 'src'}\n")
    return {"main": main, "site": site, "probe": probe}


def _run_and_judge(rt, *, env=None, script=None) -> tuple[str, list[str]]:
    import subprocess

    python = rt["main"] / ".venv" / "bin" / "python"
    script = script or rt["probe"]
    run_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **(env or {})}
    loaded = subprocess.run(
        [str(python), str(script)], env=run_env, capture_output=True, text=True, check=True
    ).stdout
    view = proof.ProcView(
        pid=1,
        argv=(str(python), str(script)),
        env={k: v for k, v in run_env.items() if k in proof.PROOF_ENV_KEYS},
        start=time.time() + 60,
        exe=os.path.realpath(python),
        mnt_ns="mnt:[1]",
    )
    verdict = proof.interpreter_source(
        view, main_root=rt["main"], own_mnt_ns="mnt:[1]", script=rt["probe"]
    )
    return loaded, verdict


def test_the_rule_agrees_with_cpython_on_a_clean_main(tmp_path):
    rt = _real_tree(tmp_path)
    loaded, verdict = _run_and_judge(rt)
    assert loaded == str(rt["main"] / "src" / "genesis" / "__init__.py")
    assert verdict == []


def test_the_rule_agrees_with_cpython_on_a_site_packages_shadow(tmp_path):
    rt = _real_tree(tmp_path)
    (rt["site"] / "genesis").mkdir()
    (rt["site"] / "genesis" / "__init__.py").write_text("")
    loaded, verdict = _run_and_judge(rt)
    assert loaded.startswith(str(rt["site"]))  # CPython took the shadow...
    assert verdict != []  # ...and the rule refuses to call it main


def test_the_rule_agrees_with_cpython_on_a_pythonpath_shadow(tmp_path):
    rt = _real_tree(tmp_path)
    shadow = tmp_path / "shadow"
    (shadow / "genesis").mkdir(parents=True)
    (shadow / "genesis" / "__init__.py").write_text("")
    loaded, verdict = _run_and_judge(rt, env={"PYTHONPATH": str(shadow)})
    assert loaded.startswith(str(shadow))
    assert verdict == ["pythonpath_unproven"]


def test_the_rule_refuses_another_trees_script_that_cpython_would_run(tmp_path):
    rt = _real_tree(tmp_path)
    other = tmp_path / "wt" / "scripts" / "probe_entry.py"
    other.parent.mkdir(parents=True)
    other.write_text(rt["probe"].read_text())
    loaded, verdict = _run_and_judge(rt, script=other)
    assert loaded == str(rt["main"] / "src" / "genesis" / "__init__.py")
    assert verdict == ["script_not_main"]  # the package is main's, the script is not



def test_the_reload_can_move_a_scratch_server_onto_the_live_db(tree):
    """The router reload sets empty values too: an empty GENESIS_DB_PATH in the
    file sends later genesis_db_path() callers to the root default (live)."""
    main = tree["main"]
    default = main / "data" / "genesis.db"
    env = {"GENESIS_DB_PATH": "/scratch.db"}
    assert proof.db_paths(env, {"GENESIS_DB_PATH": ""}, main_root=main) == [
        Path("/scratch.db"),
        default,
    ]
    # A file that moves the repo root moves the default with it.
    assert proof.db_paths({}, {"GENESIS_REPO_ROOT": "/r2"}, main_root=main) == [
        default,
        Path("/r2/data/genesis.db"),
    ]
    # And one that moves HOME re-expands a ~ path.
    env = {"GENESIS_DB_PATH": "~/b.db", "HOME": "/h1"}
    assert proof.db_paths(env, {"HOME": "/h2"}, main_root=main) == [
        Path("/h1/b.db"),
        Path("/h2/b.db"),
    ]


@pytest.mark.parametrize("entry", ["argparse.py", "json/", "logging/"])
def test_a_pythonpath_entry_shadowing_a_stdlib_module_is_unknown(tree, entry):
    """The server's own loader imports stdlib modules after startup; a PYTHONPATH
    entry precedes the stdlib for every one of them."""
    route = tree["tmp"] / "route"
    route.mkdir()
    target = route / entry.rstrip("/")
    if entry.endswith("/"):
        target.mkdir()
    else:
        target.write_text("")
    assert _source(tree, _proc(tree, env={"PYTHONPATH": str(route)})) == ["pythonpath_unproven"]


def test_a_pythonpath_entry_shadowing_an_installed_package_is_unknown(tree):
    (tree["site"] / "dotenv").mkdir()
    route = tree["tmp"] / "route"
    (route / "dotenv").mkdir(parents=True)
    assert _source(tree, _proc(tree, env={"PYTHONPATH": str(route)})) == ["pythonpath_unproven"]



def test_a_secrets_file_behind_a_nested_symlink_swapped_after_start_is_unknown(tmp_path):
    """Devin (PR review): the file's own link can be old while a link INSIDE its
    target was retargeted after start, swapping the file with an old ctime."""
    v1 = tmp_path / "v1"
    v2 = tmp_path / "v2"
    for d, body in ((v1, "GENESIS_GRAPH_TELEMETRY_DISABLED=1\n"), (v2, "OTHER=1\n")):
        d.mkdir()
        (d / "secrets.env").write_text(body)
    cur = tmp_path / "cur"
    cur.symlink_to(v1)
    entry = tmp_path / "secrets.env"
    entry.symlink_to(cur / "secrets.env")
    start = time.time() + 1
    time.sleep(1.1)
    cur.unlink()
    cur.symlink_to(v2)  # the inner link moves; the entry link and v2's file are old
    assert proof.secrets_values(entry, start) is None


def test_a_db_symlink_retargeted_after_start_cannot_prove_foreign(tmp_path):
    """Codex and Devin (PR review): a link moved after the process opened its DB
    describes a file it never opened."""
    live = tmp_path / "genesis.db"
    live.write_text("")
    bench = tmp_path / "bench.db"
    bench.write_text("")
    alias = tmp_path / "alias.db"
    alias.symlink_to(live)
    start = time.time() + 1
    time.sleep(1.1)
    alias.unlink()
    alias.symlink_to(bench)
    assert proof.db_scope([alias], live, start=start, open_files=set())[0] is False


def test_a_process_holding_the_live_db_open_is_never_foreign(tmp_path):
    live = tmp_path / "genesis.db"
    live.write_text("")
    bench = tmp_path / "bench.db"
    bench.write_text("")
    st = live.stat()
    held = {(st.st_dev, st.st_ino)}
    assert proof.db_scope([bench], live, start=time.time() + 60, open_files=held)[0] is False
    # Unreadable descriptors prove nothing either.
    assert proof.db_scope([bench], live, start=time.time() + 60, open_files=None)[0] is False
    assert proof.db_scope([bench], live, start=time.time() + 60, open_files=set())[0] is True


def test_open_file_identities_reads_the_descriptors(tmp_path):
    f = tmp_path / "x.db"
    f.write_text("")
    fd_dir = tmp_path / "proc" / "77" / "fd"
    fd_dir.mkdir(parents=True)
    (fd_dir / "3").symlink_to(f)
    st = f.stat()
    assert proof.open_file_identities(tmp_path / "proc", 77) == {(st.st_dev, st.st_ino)}
    assert proof.open_file_identities(tmp_path / "proc", 78) is None


def test_a_customizer_in_main_src_is_unknown(tree):
    """Codex (PR review): site imports sitecustomize after the .pth files extend
    sys.path, and the editable install adds main's src."""
    (tree["main"] / "src" / "sitecustomize.py").write_text("")
    assert _source(tree, _proc(tree)) == ["customize_module"]


# ── round 2: the startup inputs CPython reads that the census must see ─────


def test_a_symlinked_bin_dir_with_no_venv_cfg_above_it_is_unknown(tree):
    """CPython looks for pyvenv.cfg beside the RAW argv[0], then one level up,
    before resolving links: through a linked bin dir it finds none and runs as
    the base interpreter, whatever the link's target holds."""
    link = tree["tmp"] / "elsewhere" / "bin"
    link.parent.mkdir()
    link.symlink_to(tree["venv"] / "bin")
    proc = _proc(tree, argv=[str(link / "python"), str(tree["main"] / "scripts" / "genesis_mcp_server.py")])
    assert _source(tree, proc) == ["venv_cfg_unproven"]


def test_a_venv_cfg_beside_the_binary_wins_over_the_one_above(tree):
    (tree["venv"] / "bin" / "pyvenv.cfg").write_text("home = /usr/bin\n")
    assert _source(tree, _proc(tree)) == ["venv_cfg_unproven"]


def test_a_dotdot_argv0_is_not_main(tree):
    odd = tree["venv"] / "bin" / ".." / "bin" / "python"
    proc = _proc(tree, argv=[str(odd), str(tree["main"] / "scripts" / "genesis_mcp_server.py")])
    assert _source(tree, proc) == ["not_main_venv"]


def test_a_venv_cfg_without_a_version_cannot_locate_the_system_customizer(tree):
    cfg = tree["venv"] / "pyvenv.cfg"
    text = cfg.read_text()
    cfg.write_text(text.replace("version = 3.12.3\n", ""))
    assert _source(tree, _proc(tree)) == ["customize_unproven"]
    cfg.write_text(text.replace("version = ", "version_info = "))  # uv's spelling
    assert _source(tree, _proc(tree)) == []


def test_pythonexecutable_makes_a_venv_python_run_as_its_base(tree):
    proc = _proc(tree, env={"PYTHONEXECUTABLE": "/usr/bin/python3"})
    assert _source(tree, proc) == ["python_executable_set"]


@pytest.mark.parametrize(
    "argv_opts, env",
    [
        ([], {"PYTHONWARNINGS": "default,ignore::evilmod.W"}),
        (["-W", "ignore::evilmod.W"], {}),
        (["-Wignore::evilmod.W"], {}),
    ],
)
def test_a_dotted_warning_category_is_an_import_at_startup(tree, argv_opts, env):
    main = tree["main"]
    argv = [str(main / ".venv" / "bin" / "python"), *argv_opts, str(main / "scripts" / "genesis_mcp_server.py")]
    assert _source(tree, _proc(tree, argv=argv, env=env)) == ["warnings_import"]


def test_a_plain_warning_filter_imports_nothing(tree):
    env = {"PYTHONWARNINGS": "ignore::DeprecationWarning"}
    assert _source(tree, _proc(tree, env=env)) == []


@pytest.mark.parametrize("entry", ["inspect.py", "dotenv/", "evil-1.0.dist-info/"])
def test_the_script_dir_may_not_shadow_a_runtime_import(tree, entry):
    """sys.path[0] precedes the stdlib and site-packages for every import the
    server makes at runtime, so a stray stdlib, installed or metadata name there
    replaces the real one."""
    (tree["site"] / "dotenv").mkdir()
    target = tree["main"] / "scripts" / entry.rstrip("/")
    if entry.endswith("/"):
        target.mkdir()
    else:
        target.write_text("")
    assert _source(tree, _proc(tree)) == ["script_dir_shadow"]


def test_package_metadata_on_pythonpath_is_unknown(tree):
    route = tree["tmp"] / "route"
    (route / "plugin-1.0.dist-info").mkdir(parents=True)
    assert _source(tree, _proc(tree, env={"PYTHONPATH": str(route)})) == ["pythonpath_unproven"]


def test_code_read_from_stdin_is_not_the_named_script(tree):
    """`python - script.py` runs stdin, with the script name only as an argument."""
    main = tree["main"]
    argv = [str(main / ".venv" / "bin" / "python"), "-", str(main / "scripts" / "genesis_mcp_server.py")]
    assert _source(tree, _proc(tree, argv=argv)) == ["script_not_absolute"]


# ── round 3 (#3510): allowlists, the Genesis home, finders ────────────────


@pytest.mark.parametrize(
    "opts",
    [['-mgenesis'], ["--check-hash-based-pycs", "always"], ["-uB"], ["--"]],
)
def test_an_interpreter_option_outside_the_allowlist_is_unknown(tree, opts):
    """An option these rules do not model can shift what reads as the script."""
    main = tree["main"]
    argv = [str(main / ".venv" / "bin" / "python"), *opts, str(main / "scripts" / "genesis_mcp_server.py")]
    assert "interpreter_options_unmodelled" in _source(tree, _proc(tree, argv=argv))


@pytest.mark.parametrize("opts", [["-I"], ["-P"], ["-u", "-B"], ["-X", "dev"], ["-Xdev"], ["-O"]])
def test_allowlisted_interpreter_options_pass(tree, opts):
    """Every flag the repo's own launch sites use (measured: -I, -P, -X, -O)."""
    main = tree["main"]
    argv = [str(main / ".venv" / "bin" / "python"), *opts, str(main / "scripts" / "genesis_mcp_server.py")]
    assert _source(tree, _proc(tree, argv=argv)) == []


def test_an_unmodelled_python_variable_is_unknown(tree):
    proc = _proc(tree, env={"PYTHONPYCACHEPREFIX": "/elsewhere"})
    assert _source(tree, proc) == ["python_env_unmodelled"]


def test_inert_python_variables_pass(tree):
    proc = _proc(tree, env={"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    assert _source(tree, proc) == []


def test_a_finder_redirecting_a_module_the_server_imports_is_unknown(tree, monkeypatch):
    """The census host runs the same code, so its imports are the server's."""
    monkeypatch.setattr(proof, "_host_imports", lambda: frozenset({"anomaly_detector"}))
    assert _source(tree, _proc(tree)) == ["unknown_pth:__editable__.cloudsleuth-0.1.0.pth"]


def test_a_finder_for_a_module_the_server_never_imports_passes(tree, monkeypatch):
    monkeypatch.setattr(proof, "_host_imports", lambda: frozenset({"json"}))
    assert _source(tree, _proc(tree)) == []


def test_the_genesis_home_is_resolved_as_genesis_env_resolves_it(tmp_path):
    home = tmp_path / "u"
    assert proof.genesis_home_of({"HOME": str(home)}) == home / ".genesis"
    assert proof.genesis_home_of({"HOME": str(home), "GENESIS_HOME": "~/alt"}) == home / "alt"
    # A proven secrets file's value wins: the router reload applies it with override.
    assert proof.genesis_home_of({"HOME": str(home)}, {"GENESIS_HOME": "/x"}) == Path("/x")
    assert proof.genesis_home_of({"GENESIS_HOME": "relative"}) is None


def test_same_home_compares_files_or_real_paths(tmp_path):
    a = tmp_path / "home"
    a.mkdir()
    link = tmp_path / "link"
    link.symlink_to(a)
    assert proof.same_home(link, a)
    assert proof.same_home(tmp_path / "not-yet", tmp_path / "not-yet")  # neither exists
    assert not proof.same_home(tmp_path / "other", a)
    assert not proof.same_home(None, a)
