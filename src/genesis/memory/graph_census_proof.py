"""What the graph census can PROVE about another process: code source, kill
switch and database scope.

The census (``graph_census.py``) may start the FalkorDB cutover clock only on
evidence, so every rule here answers "unknown" rather than guessing. Each rule
is a pure function over one process's ``/proc`` facts (``ProcView``) and files
on disk; ``graph_census`` gathers the facts and records the verdicts.

**Import source, for an interpreter that does not insert its own ``src``**
(``interpreter_source``). CPython builds ``sys.path`` for
``<venv>/bin/python /abs/script.py`` as: the script's real directory,
``PYTHONPATH`` entries, the stdlib (from ``pyvenv.cfg``'s ``home``), then the
venv's site-packages and whatever its ``.pth`` files add or run; ``site`` also
imports ``sitecustomize``/``usercustomize`` from that path. The venv is
identified by the directory of ``argv[0]``, never by the binary: every venv's
``python`` is a symlink chain to the same system interpreter (MEASURED on a
live install, 2026-10-20), so the binary's realpath proves nothing. A name the
startup imports (``genesis``, a customize module, anything a ``.pth`` line or
the system ``sitecustomize`` imports) found in a ``PYTHONPATH`` entry or beside
the script would shadow the real one, so it makes the source unknown.

Stated residuals: the venv's site-packages (its ``.pth`` files, and any
``genesis`` or customize module installed there) is trusted as it reads NOW,
with no change-time rule, because every update reruns ``pip install -e`` and
rewrites it: such a rule would mark every running server unknown after every
update (MEASURED 2026-10-20: 16 of 16 live servers started before the last
rewrite). Owner ruling, 2026-10-20: accepted. A package installed there when a
server started and removed later is therefore not seen. The window this leaves: a
``pip install -e`` from a worktree into main's venv, a server started then, and
an update that rewrites the install back to main before the census reads it
would read as main. A standing procedure already forbids that install; a ``PYTHONPATH`` entry is proven only by its change time
(``st_ctime``, which userspace cannot set) being earlier than the process start;
the stdlib directory is trusted (root-owned on a packaged interpreter).

**Kill switch** (``secrets_values`` + ``switch_off``): what the server runs
with, not the startup loader alone. The memory lifespan's router reloads the
secrets file with ``override=True``, so a key the file carries beats the
exec-time value (owner ruling, 2026-10-20). The file counts only if it exists
and neither it nor any symlink on its path changed since the process started;
a missing file is UNKNOWN, never "enabled", and so is a startup value whose
file cannot be proven, since that file can override it. One exception
(``carry_secrets``): a file changed only in keys the census does not read is
carried, when the latest census row proved this same server (pid and start)
with the same census-key values; rows record that digest, never a value.

**Database scope** (``db_paths`` + ``db_scope``): the primary database is fixed
when the server script is imported (exec env, else ``<root>/data``); a
``GENESIS_DB_PATH`` in its proven file becomes a second one after the router
reload. A server is foreign only when every database it may use differs from
the live one by file identity ``(st_dev, st_ino)``, so a hardlink or bind mount
of the live database is never mistaken for a scratch one. Caveat: on overlayfs the
inode can differ for one file across layers; an unknown or missing path is
never foreign. A foreign process can still reach the live graph ENGINE (its
socket comes from ``GENESIS_HOME``, not the database); its traversals serve no
live memory, so they are not live traffic, by the same reasoning that excludes
replay.

**Threat model: accidental drift** (owner ruling, 2026-10-20). The rules catch
what goes wrong on a working install: a server started before a deploy or a
secrets edit, one launched from a worktree or another venv, a sandbox
``PYTHONPATH``, a stray file beside the script, a switch or database set at
launch. They do not try to defeat someone with shell access deliberately
building inputs, who could as easily edit main's code. Without a record the
process writes at its own start (ruled out for now; tracked as a redesign),
these deliberate constructions stay as residuals:
- an ordinary (non-symlink) ancestor directory of the secrets file swapped for
  a pre-built tree. A ctime rule on ancestors would fail every live server
  (MEASURED: the home and checkout directories change constantly);
- forged ``.pyc`` files, or a ``PYTHONPYCACHEPREFIX`` / ``-X pycache_prefix``;
- files added beside the script or in site-packages after the start and
  removed before the census reads;
- the stdlib or site-packages upgraded after the start;
- a secrets edit landing between the change-time check and the read: at most
  one census row reads the wrong switch, and the next census sees the new
  change time, reads unknown, and resets the clock;
- a venv process in another mount namespace (the test sandbox, MEASURED: 4 of 4
  such processes) is judged on the host checkout's history, because its paths
  are not ours to resolve; reading it unknown would reset the clock on every
  census that lands during a sandboxed test run.

Every fail-closed rule here was measured against the live processes before it
shipped (owner ruling, 2026-10-20): a dirty row RESETS the 14-day clock, so a
rule that routine processes trip would make the cutover unreachable.

No reason code carries an environment value or a path from another process.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

#: A row's ``source`` when ``interpreter_source`` proved the main checkout.
SOURCE_MAIN = "main"
#: The only environment keys the census keeps from another process.
PROOF_ENV_KEYS = (
    "HOME",
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONPLATLIBDIR",
    "PYTHONEXECUTABLE",
    "PYTHONWARNINGS",
    "SECRETS_PATH",
    "GENESIS_REPO_ROOT",
    "GENESIS_DB_PATH",
    "GENESIS_GRAPH_TELEMETRY_DISABLED",
    "GENESIS_HOME",
)
_SWITCH = "GENESIS_GRAPH_TELEMETRY_DISABLED"
#: PYTHON* variables this module models (each has its own rule above or below).
_PY_ENV_MODELLED = frozenset(
    {"PYTHONPATH", "PYTHONHOME", "PYTHONPLATLIBDIR", "PYTHONEXECUTABLE", "PYTHONWARNINGS"}
)
#: PYTHON* variables that cannot change which code a script run loads. Any other
#: PYTHON* variable makes the source unknown: an allowlist, because CPython's
#: set is small and stable, and a denylist only ever lists the vectors found so far.
_PY_ENV_INERT = frozenset(
    {
        "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE", "PYTHONIOENCODING", "PYTHONUTF8",
        "PYTHONHASHSEED", "PYTHONFAULTHANDLER", "PYTHONNOUSERSITE", "PYTHONSAFEPATH",
        "PYTHONTRACEMALLOC", "PYTHONMALLOC", "PYTHONCOERCECLOCALE", "PYTHONASYNCIODEBUG",
        "PYTHONDEVMODE", "PYTHONSTARTUP",  # PYTHONSTARTUP: interactive sessions only
    }
)
#: Interpreter flags (before the script) that cannot change which code loads.
_PY_FLAGS_INERT = frozenset(
    {"-I", "-P", "-u", "-B", "-E", "-s", "-S", "-O", "-OO", "-q", "-b", "-bb", "-R"}
)
#: setuptools' ``distutils-precedence.pth``, byte for byte after strip.
_DISTUTILS_LINE = (
    "import os; var = 'SETUPTOOLS_USE_DISTUTILS'; enabled = os.environ.get(var, 'local')"
    " == 'local'; enabled and __import__('_distutils_hack').add_shim();"
)
_FINDER_LINE = re.compile(r"import (__editable___\w+_finder); \1\.install\(\)")
#: Interpreter options that consume the next argument (``python -X dev script``).
_PY_OPTS_WITH_VALUE = frozenset({"-X", "-W", "-Q"})
_CUSTOMIZE = ("sitecustomize", "usercustomize")


@dataclass(frozen=True)
class ProcView:
    """One process as ``/proc`` shows it. ``env`` holds ``PROOF_ENV_KEYS`` only."""

    pid: int
    argv: tuple[str, ...]
    env: dict[str, str]
    start: float
    exe: str | None
    mnt_ns: str | None


def expand_home(value: str, env: dict[str, str]) -> Path | None:
    """``value`` as THAT process would expand it: ``~`` uses its own ``HOME``.
    None when it cannot be resolved from here (a relative path, or ``~`` with
    no ``HOME`` in its environment)."""
    if value.startswith("~"):
        home = env.get("HOME")
        if not home or not (value == "~" or value.startswith("~/")):
            return None
        value = home + value[1:]
    path = Path(value)
    return path if path.is_absolute() else None


def _shadows(directory: Path, names: set[str]) -> bool:
    """Whether ``directory`` holds something importable under one of ``names``."""
    try:
        entries = os.listdir(directory)
    except OSError:
        return True
    for entry in entries:
        stem = entry.split(".", 1)[0]
        # __pycache__ and dist-info style names are never import targets.
        if stem not in names or not stem.isidentifier() or stem == "__pycache__":
            continue
        if (
            entry == stem
            or entry.endswith((".py", ".pyc"))
            or (entry.endswith(".so") and entry.startswith(stem + "."))
        ):
            return True
    return False


def _dotted_warning_category(argv: tuple[str, ...], env: dict[str, str]) -> bool:
    """Whether a ``-W`` option or ``PYTHONWARNINGS`` names a dotted category:
    CPython imports that module while it processes the warning filters, before
    ``site`` runs (``warnings._getcategory``)."""
    filters = env.get("PYTHONWARNINGS", "").split(",")
    i = 1
    while i < len(argv) and argv[i].startswith("-") and argv[i] not in ("-m", "-c", "-"):
        if argv[i] == "-W" and i + 1 < len(argv):
            filters.append(argv[i + 1])
        elif argv[i].startswith("-W") and len(argv[i]) > 2:
            filters.append(argv[i][2:])
        i += 2 if argv[i] in _PY_OPTS_WITH_VALUE else 1
    return any("." in (f.split(":") + ["", "", ""])[2] for f in filters)


def _host_imports() -> frozenset[str]:
    """Top-level modules THIS process has imported."""
    return frozenset(n.split(".", 1)[0] for n in list(sys.modules))


def options_unmodelled(argv: tuple[str, ...]) -> bool:
    """Whether the interpreter options before the script include one these rules
    do not model: anything but the inert flags, ``-X``/``-W`` (separate or
    attached; ``-W`` is checked for imports on its own) and the ``-m``/``-c``/``-``
    terminators. ``-mgenesis`` (attached) or ``--check-hash-based-pycs always``
    would otherwise shift what is read as the script."""
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        token = argv[i]
        if token in ("-m", "-c", "-"):
            return False
        if token in _PY_FLAGS_INERT:
            i += 1
        elif token in ("-X", "-W"):
            i += 2
        elif token.startswith(("-X", "-W")) and len(token) > 2:
            i += 1
        else:
            return True
    return False


def python_env_unmodelled(env: dict[str, str]) -> bool:
    """Whether ``env`` sets a PYTHON* variable outside the modelled and inert sets."""
    return any(
        k.startswith("PYTHON") and k not in _PY_ENV_MODELLED and k not in _PY_ENV_INERT
        for k in env
    )


def genesis_home_of(env: dict[str, str], file_vals: dict | None = None) -> Path | None:
    """The Genesis home that process resolves, as ``genesis.env.genesis_home``
    does: ``GENESIS_HOME``, else ``<HOME>/.genesis``. A proven secrets file's
    values are laid over the launch env, because the router reload applies them
    with ``override=True``. None when it cannot be resolved from here."""
    merged = {**env, **(file_vals or {})}
    if merged.get("GENESIS_HOME"):
        return expand_home(merged["GENESIS_HOME"], merged)
    home = merged.get("HOME")
    if home is None:
        import pwd

        home = pwd.getpwuid(os.getuid()).pw_dir  # what Path.home() falls back to
    return Path(home) / ".genesis" if os.path.isabs(home) else None


def same_home(path: Path | None, host_home: Path) -> bool:
    """Whether ``path`` is the census host's Genesis home: the same file when
    both exist, else the same real path (a home not created yet)."""
    if path is None:
        return False
    try:
        return os.path.samefile(path, host_home)
    except OSError:
        return os.path.realpath(path) == os.path.realpath(host_home)


def _has_dist_metadata(directory: Path) -> bool:
    """Whether ``directory`` holds package metadata. ``importlib.metadata``
    walks every ``sys.path`` entry, and an entry-point plugin loader (pydantic's,
    say) imports whatever a ``*.dist-info`` there names."""
    try:
        return any(n.endswith((".dist-info", ".egg-info")) for n in os.listdir(directory))
    except OSError:
        return True


def _finder_mapping(site: Path, module: str) -> dict | None:
    try:
        tree = ast.parse((site / f"{module}.py").read_text())
    except (OSError, SyntaxError, ValueError):
        return None
    for node in tree.body:
        target = (
            node.target
            if isinstance(node, ast.AnnAssign)
            else (
                node.targets[0] if isinstance(node, ast.Assign) and len(node.targets) == 1 else None
            )
        )
        if isinstance(target, ast.Name) and target.id == "MAPPING" and node.value is not None:
            try:
                value = ast.literal_eval(node.value)
            except ValueError:
                return None
            return value if isinstance(value, dict) else None
    return None


def _pth_verdict(
    site: Path, name: str, main_src: str, imports: set[str], added: list[str],
    runtime: frozenset[str] = frozenset(),
) -> bool:
    """Whether one ``.pth`` file is a known shape that cannot load other genesis
    code. Adds every module it imports to ``imports`` and every directory it puts
    on ``sys.path`` to ``added``."""
    try:
        lines = (site / name).read_text().splitlines()
    except (OSError, ValueError):
        return False
    # site.addpackage: a comment only when the RAW line starts with "#", a blank
    # line by strip(), an import line by its raw prefix (exec'd whole), and any
    # other line is a path joined to the site dir after rstrip().
    for raw in lines:
        if raw.startswith("#") or not raw.strip():
            continue
        line = raw.strip()
        if raw.startswith(("import ", "import\t")) and line == _DISTUTILS_LINE:
            imports.add("_distutils_hack")
            continue
        finder = _FINDER_LINE.fullmatch(line) if raw.startswith("import ") else None
        if finder:
            module = finder.group(1)
            mapping = _finder_mapping(site, module)
            if mapping is None:
                return False
            target = mapping.get("genesis")
            if target is not None and os.path.realpath(target) != os.path.join(main_src, "genesis"):
                return False
            # A finder serving a module the server itself imports (the census
            # host runs the same code, so its imports are that set) redirects
            # that dependency. Other keys (an unrelated tool's package) cannot
            # be reached by the server's code.
            if any(k != "genesis" and k in runtime for k in mapping):
                return False
            imports.add(module)
            continue
        if raw.startswith(("import ", "import\t")):
            return False
        if os.path.realpath(os.path.join(site, raw.rstrip())) != main_src:
            return False
        added.append(main_src)
    return True


def _script(argv: tuple[str, ...]) -> str | None:
    """The script a ``python [opts] script`` command runs; None for ``-m``/``-c``."""
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        if argv[i] in ("-m", "-c", "-"):
            return None  # a module, a command, or code read from stdin
        i += 2 if argv[i] in _PY_OPTS_WITH_VALUE else 1
    return argv[i] if i < len(argv) else None


def interpreter_source(
    proc: ProcView, *, main_root: Path, own_mnt_ns: str | None, script: Path | None = None
) -> list[str]:
    """``[]`` when the process provably loads genesis from ``main_root``'s src,
    else the reason codes. See the module docstring for the rule. ``script``,
    when given, is the entry script it must run: the script's own code (its
    secrets loader, its startup) is part of what decides the switch and DB."""
    if own_mnt_ns is None or proc.mnt_ns != own_mnt_ns:
        return ["other_mount_ns"]  # its paths are not ours to resolve
    argv0 = proc.argv[0] if proc.argv else ""
    if not os.path.isabs(argv0):
        return ["argv0_not_absolute"]
    venv = main_root / ".venv"
    bin_dir = os.path.dirname(argv0)
    if ".." in Path(argv0).parts or os.path.realpath(bin_dir) != os.path.realpath(venv / "bin"):
        return ["not_main_venv"]
    reasons: list[str] = []
    # CPython looks for pyvenv.cfg beside the RAW executable path, then one level
    # up, before resolving any symlink: the file it finds decides the venv.
    found = next(
        (c for c in (Path(bin_dir) / "pyvenv.cfg", Path(bin_dir).parent / "pyvenv.cfg") if c.is_file()),
        None,
    )
    try:
        cfg_is_main = found is not None and os.path.samefile(found, venv / "pyvenv.cfg")
    except OSError:
        cfg_is_main = False
    if not cfg_is_main:
        reasons.append("venv_cfg_unproven")
    cfg: dict[str, str] = {}
    try:
        for line in (venv / "pyvenv.cfg").read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep:
                cfg[key.strip()] = value.strip()
    except OSError:
        pass
    executable = cfg.get("executable")
    if cfg.get("include-system-site-packages") != "false" or not executable or not cfg.get("home"):
        if "venv_cfg_unproven" not in reasons:
            reasons.append("venv_cfg_unproven")
    elif proc.exe is None or os.path.realpath(proc.exe) != os.path.realpath(executable):
        reasons.append("exe_mismatch")
    if "PYTHONHOME" in proc.env or "PYTHONPLATLIBDIR" in proc.env:
        reasons.append("python_home_set")
    if "PYTHONEXECUTABLE" in proc.env:
        reasons.append("python_executable_set")  # makes a venv python run as its base
    if _dotted_warning_category(proc.argv, proc.env):
        reasons.append("warnings_import")
    if options_unmodelled(proc.argv):
        reasons.append("interpreter_options_unmodelled")
    if python_env_unmodelled(proc.env):
        reasons.append("python_env_unmodelled")

    sites = sorted((venv / "lib").glob("python*/site-packages"))
    imports: set[str] = {"genesis", *_CUSTOMIZE}
    if len(sites) != 1:
        reasons.append("site_packages_ambiguous")
    else:
        site = sites[0]
        main_src = os.path.realpath(main_root / "src")
        try:
            names = sorted(
                n for n in os.listdir(site) if n.endswith(".pth") and not n.startswith(".")
            )
        except OSError:
            names = None
            reasons.append("site_packages_ambiguous")
        added: list[str] = []
        for name in names or []:
            if not _pth_verdict(site, name, main_src, imports, added, _host_imports()):
                reasons.append(f"unknown_pth:{name}")
        # site imports the customize modules from sys.path AFTER the .pth files
        # extend it, so a directory a .pth adds is searched too (main src included:
        # an untracked src/sitecustomize.py would run before genesis is imported).
        if any(_shadows(Path(d), set(_CUSTOMIZE)) for d in added):
            reasons.append("customize_module")
        # A path-line .pth puts main src AFTER site-packages on sys.path (and a
        # finder install runs after PathFinder), so a genesis there wins.
        if _shadows(site, {"genesis"}):
            reasons.append("site_packages_shadow")
        if _shadows(site, set(_CUSTOMIZE)):
            reasons.append("customize_module")

    # The system sitecustomize ``site`` imports (Debian's runs an apport hook):
    # whatever it imports by name could be shadowed through PYTHONPATH.
    home = cfg.get("home")
    version = cfg.get("version") or cfg.get("version_info") or ""
    major_minor = ".".join(version.split(".")[:2])
    if home and not major_minor:
        reasons.append("customize_unproven")  # cannot locate the system sitecustomize
    elif home:
        stdlib_custom = Path(home).parent / "lib" / f"python{major_minor}" / "sitecustomize.py"
        if stdlib_custom.exists():
            try:
                tree = ast.parse(stdlib_custom.read_text())
            except (OSError, SyntaxError, ValueError):
                reasons.append("customize_unproven")
            else:
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        imports.update(a.name.split(".")[0] for a in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        imports.add(node.module.split(".")[0])

    # A PYTHONPATH entry precedes the stdlib and site-packages for EVERY import,
    # not just the startup's: the server's own loader (dotenv, argparse, ...)
    # could be replaced. So it may hold no stdlib or installed top-level name.
    # Left: an optional import of a package that is not installed.
    reachable = set(imports) | set(sys.stdlib_module_names)
    if len(sites) == 1:
        # Unlistable is already "site_packages_ambiguous" above, so the row is dirty.
        with contextlib.suppress(OSError):
            reachable.update(n.split(".", 1)[0] for n in os.listdir(sites[0]))
    raw_path = proc.env.get("PYTHONPATH")
    if raw_path is not None:
        for entry in raw_path.split(os.pathsep):
            path = Path(entry)
            try:
                ok = (
                    entry != ""  # an empty entry is the cwd at start, unknowable now
                    and path.is_absolute()
                    and path.is_dir()
                    and _unchanged_since(path, proc.start)
                    and not _shadows(path, reachable)
                    and not _has_dist_metadata(path)
                )
            except OSError:
                ok = False
            if not ok:
                reasons.append("pythonpath_unproven")
                break

    entry = _script(proc.argv)
    if entry is None or not os.path.isabs(entry):
        reasons.append("script_not_absolute")
    else:
        if script is not None and os.path.realpath(entry) != os.path.realpath(script):
            reasons.append("script_not_main")
        # sys.path[0] precedes the stdlib and site-packages for every import the
        # server makes at runtime (dotenv, argparse, ...), so the script's
        # directory may hold no stdlib or installed name, and no package metadata.
        script_dir = Path(os.path.dirname(os.path.realpath(entry)))
        if _shadows(script_dir, reachable) or _has_dist_metadata(script_dir):
            reasons.append("script_dir_shadow")
    return reasons


def secrets_file(env: dict[str, str], *, main_root: Path) -> Path | None:
    """The secrets file that process loads, chosen as ``genesis.env.secrets_path``
    chooses it: ``SECRETS_PATH``, else ``GENESIS_REPO_ROOT``'s, else main's."""
    if env.get("SECRETS_PATH"):
        return expand_home(env["SECRETS_PATH"], env)
    if env.get("GENESIS_REPO_ROOT"):
        root = expand_home(env["GENESIS_REPO_ROOT"], env)
        return None if root is None else root / "secrets.env"
    return main_root / "secrets.env"


def _links_unchanged_since(path: Path, start: float, _depth: int = 0) -> bool:
    """No symlink met while resolving ``path`` changed after ``start``: every
    link on the way, INCLUDING those inside a link's own target (a retargeted
    link two levels down swaps the file as surely as one on the lexical path)."""
    if _depth > 40:  # the kernel's own symlink-loop limit
        return False
    parts = path.parts
    probe = Path(path.anchor)
    for i, part in enumerate(parts[1:], start=1):
        probe = probe / part
        if probe.is_symlink():
            if probe.lstat().st_ctime >= start:
                return False
            target = Path(os.readlink(probe))
            resolved = target if target.is_absolute() else probe.parent / target
            return _links_unchanged_since(resolved.joinpath(*parts[i + 1 :]), start, _depth + 1)
    return True


def _unchanged_since(path: Path, start: float) -> bool:
    """Neither ``path`` nor any symlink met resolving it changed after ``start``."""
    return _links_unchanged_since(path, start) and path.stat().st_ctime < start


def secrets_values(path: Path | None, start: float) -> dict[str, str] | None:
    """The file's values as the process loaded them, or None when that cannot be
    proven: missing, unreadable, or changed (it or a symlink on its path) since
    the process started."""
    if path is None:
        return None
    try:
        if not _unchanged_since(path, start):
            return None
        from dotenv import dotenv_values

        # Raw: interpolating ${VAR} here would use the census's environment, not
        # that process's. A value that needs it reads as unknown below.
        return {k: v for k, v in dotenv_values(path, interpolate=False).items() if v is not None}
    except Exception:
        return None


#: Every secrets.env key the census reads (the switch, `db_paths`,
#: `genesis_home_of`, `secrets_file`). A proven row records a digest over these
#: and nothing else, so an edit to any OTHER key leaves a server's census
#: values exactly as proven.
CENSUS_SECRET_KEYS = (
    _SWITCH,
    "GENESIS_DB_PATH",
    "GENESIS_REPO_ROOT",
    "HOME",
    "GENESIS_HOME",
    "SECRETS_PATH",
)


def secrets_digest(vals: dict[str, str]) -> str:
    """A digest of ``vals``' census-relevant keys: sorted ``(key, value)`` pairs,
    an absent key simply absent. Only the digest is ever written to a row."""
    pairs = sorted((k, vals[k]) for k in CENSUS_SECRET_KEYS if k in vals)
    return hashlib.sha256(json.dumps(pairs).encode()).hexdigest()


def changed_secrets_values(path: Path | None, start: float) -> dict[str, str] | None:
    """The file's CURRENT census-relevant values, when its one fault is having
    changed since ``start``: present, readable, and every symlink on its path
    older than ``start``. None for any other fault (missing, unreadable, a
    swapped link), and for a file that has not changed (`secrets_values`
    proves that one)."""
    if path is None:
        return None
    try:
        if not _links_unchanged_since(path, start) or not path.is_file():
            return None
        if path.stat().st_ctime < start:
            return None
        from dotenv import dotenv_values

        vals = dotenv_values(path, interpolate=False)
        return {k: v for k in CENSUS_SECRET_KEYS if (v := vals.get(k)) is not None}
    except Exception:
        return None


def carry_secrets(
    path: Path | None,
    start: float,
    proven_digest: str | None,
    *,
    env: dict[str, str],
    main_root: Path,
) -> dict[str, str] | None:
    """The values a server whose secrets file changed since it started is
    running with, when that can still be proven: an earlier census row for this
    same server (pid and start) proved a file whose census-relevant values equal
    the current file's. Then every load the server can make, at startup or a
    later router reload, yields exactly these values.

    Refused when the current values could move the secrets file itself: the
    file names ``SECRETS_PATH`` or ``GENESIS_REPO_ROOT``, or the path derived
    with its values laid over the launch environment (as a reload's
    ``expanduser`` sees a ``HOME`` it set) is not this file. A later reload would
    otherwise read a file nobody proved.

    Residuals, stated:
    - a server whose startup router build failed reloads lazily at an arbitrary
      later moment, so a census-key edit REVERTED between two censuses can leave
      it holding the intermediate value. That needs both a broken startup and an
      edit-and-revert inside one census interval (about two if a census write
      crashed, since only the latest row is read);
    - the model is three writers of a memory server's environment: its launch,
      the MCP loader and the router reload. litellm's import-time
      ``load_dotenv()`` (``override=False``, a ``.env`` walked up from its own
      package) is a fourth that sets only unset keys; this is disproved by any
      other env writer reachable from a memory server."""
    if not proven_digest:
        return None
    current = changed_secrets_values(path, start)
    if current is None or "SECRETS_PATH" in current or "GENESIS_REPO_ROOT" in current:
        return None
    if secrets_file({**env, **current}, main_root=main_root) != path:
        return None
    return current if secrets_digest(current) == proven_digest else None


def switch_off(env: dict[str, str], file_vals: dict[str, str] | None) -> bool | None:
    """The kill switch a memory server actually runs with, or None.

    Two loads decide it. At startup the MCP loader copies a file value only for a
    key not already set (``scripts/genesis_mcp_server.py``); then the memory
    lifespan's router reloads the same file with ``override=True``
    (``routing/standalone.py``), so a key the file carries wins from there on, and
    telemetry reads the switch on every call. So: unprovable file -> unknown;
    the two phases disagree -> unknown; else their common value."""
    if file_vals is None:
        return None
    file_value = file_vals.get(_SWITCH)
    if file_value is not None and "${" in file_value:
        return None
    at_start = env.get(_SWITCH, file_value or None)
    running = file_value if file_value is not None else env.get(_SWITCH)
    start_off, run_off = at_start == "1", running == "1"
    return run_off if start_off == run_off else None


def db_paths(
    env: dict[str, str], file_vals: dict[str, str] | None, *, main_root: Path
) -> list[Path | None]:
    """Every database a memory server may use, its primary first; None for one
    that cannot be resolved from here.

    The primary connection's path (``_DEFAULT_DB``) is fixed when the server
    script is imported, BEFORE any secrets load: the exec-time
    ``GENESIS_DB_PATH``, else ``<root>/data/genesis.db``. The lifespan's router
    later reloads the file with ``override=True``, so a ``GENESIS_DB_PATH`` the
    file carries becomes what any later ``genesis_db_path()`` call resolves: a
    second database. An unprovable file leaves that second one unknown.

    One resolver serves both, mirroring ``genesis.env.genesis_db_path`` and
    ``repo_root`` over the environment each phase sees: the launch environment,
    then that environment with every key the file sets laid over it (the reload
    sets empty values too, and a file may move ``GENESIS_REPO_ROOT`` or ``HOME``)."""
    primary = _resolve_db(env, main_root=main_root)
    if file_vals is None:
        return [primary, None]
    after_reload = _resolve_db({**env, **file_vals}, main_root=main_root)
    return [primary] if after_reload == primary else [primary, after_reload]


def _resolve_db(env: dict[str, str], *, main_root: Path) -> Path | None:
    """``genesis_db_path()`` as a process with ``env`` resolves it, or None."""
    if any("${" in env.get(k, "") for k in ("GENESIS_DB_PATH", "GENESIS_REPO_ROOT", "HOME")):
        return None
    value = env.get("GENESIS_DB_PATH")
    if value:
        return expand_home(value, env)
    if env.get("GENESIS_REPO_ROOT"):
        root = expand_home(env["GENESIS_REPO_ROOT"], env)
        return None if root is None else root / "data" / "genesis.db"
    return main_root / "data" / "genesis.db"


def db_scope(
    paths: list[Path | None],
    live: Path,
    *,
    start: float,
    open_files: set[tuple[int, int]] | None,
) -> tuple[bool, list[int] | None]:
    """``(foreign, [st_dev, st_ino] of the primary)``.

    Foreign only when ALL of these hold: every database the process may use
    stats and differs from the live one; no symlink met resolving those paths
    changed since the process started (a retargeted link would describe a file
    it never opened); and the process holds no open file that IS the live
    database (``open_files``: the ``(st_dev, st_ino)`` of its open descriptors,
    None when unreadable). The open handle is what a long-lived connection
    actually uses; a path is only evidence of what it opened. Anything unknown
    counts as the live database."""
    try:
        ours = live.stat()
    except OSError:
        return False, None
    idents: list[list[int] | None] = []
    for path in paths:
        try:
            st = path.stat() if path is not None else None
        except OSError:
            st = None
        idents.append(None if st is None else [st.st_dev, st.st_ino])
    live_ident = [ours.st_dev, ours.st_ino]
    foreign = (
        bool(idents)
        and all(i is not None and i != live_ident for i in idents)
        and all(_links_unchanged_since(p, start) for p in paths if p is not None)
        and open_files is not None
        and (ours.st_dev, ours.st_ino) not in open_files
    )
    return foreign, idents[0] if idents else None


def open_file_identities(proc_root: Path, pid: int) -> set[tuple[int, int]] | None:
    """``(st_dev, st_ino)`` of every regular file ``pid`` holds open, or None
    when its descriptors cannot be listed (another user's process, or gone)."""
    fd_dir = proc_root / str(pid) / "fd"
    try:
        fds = os.listdir(fd_dir)
    except OSError:
        return None
    found: set[tuple[int, int]] = set()
    for fd in fds:
        try:
            st = os.stat(fd_dir / fd)
        except OSError:
            continue  # closed between listing and stat
        found.add((st.st_dev, st.st_ino))
    return found
