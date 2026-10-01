"""scripts/lib/deploy_status.sh: the files under scripts/ the running server consumes (#2601).

The server also imports or runs a few files under scripts/ on demand, and
deploy_status.sh names them in two lists:

  * `_RUNTIME_RELOAD_SCRIPTS`: imported once and kept. They join `_RUNTIME_PATHS`,
    so a pull of one is PENDING for a restart and a `deploy` of one restarts.
  * `_RUNTIME_FRESH_SCRIPTS`: read or run afresh on every use. A pull of one is
    live at once, so it asks for no restart, but it still voids the validation
    bracket, and an uncommitted edit to one reads as a runtime edit.

Two kinds of test: behaviour through the real deploy script on the station
fixture, and the lists against the call sites in src/genesis.

What the call-site scan reads (every shape below has a case in
test_the_scan_sees_each_shape_it_claims):
  * a `/`-join chain carrying a "scripts" constant, a constant with a "scripts/"
    prefix, or a module-level name bound to "scripts" (the parts after it name
    the target; a computed part is recorded as `*`, which nothing covers);
  * a call whose positional arguments, or a list/tuple among them, carry one;
  * a string, bytes or f-string constant that is nothing but a path into
    scripts/, or a dotted module name under `scripts.`;
  * a path-only constant naming a file that exists under scripts/ without the
    prefix (the hook launcher, .claude/hooks/genesis-hook, adds "scripts/");
  * a bare import of a module that exists only under scripts/, which works
    after a call site puts that directory on sys.path;
  * an import of `scripts.<...>` as a package (the server's working directory
    is the checkout, and `python -m` puts that first on sys.path).
What it cannot see: a scripts/ path inside longer text (a message, or a shell
snippet the server runs), a path assembled another way, or a script the server
reaches only through a systemd unit it starts or a Claude Code session it
launches (hooks and MCP servers run there, not in the server). The scan covers
scripts/ only; the one listed file outside it, the hook launcher
.claude/hooks/genesis-hook, is in _OUTSIDE_SCRIPTS with the call site that runs
it, and that call site is checked.
"""

from __future__ import annotations

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_scripts._deploy_station import REPO
from tests.test_scripts._deploy_station import advance_upstream as _advance_upstream
from tests.test_scripts._deploy_station import git as _git
from tests.test_scripts._deploy_station import restarted as _restarted
from tests.test_scripts._deploy_station import run as _run

pytestmark = pytest.mark.skipif(sys.platform.startswith("win"), reason="bash-only")

DEPLOY_STATUS = REPO / "scripts" / "lib" / "deploy_status.sh"


def _bash_array(name: str) -> list[str]:
    """An array from deploy_status.sh as bash itself reads it."""
    r = subprocess.run(
        ["bash", "-c", f'source "$1" && printf "%s\\n" "${{{name}[@]}}"', "_", str(DEPLOY_STATUS)],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in r.stdout.splitlines() if line]


def _listed() -> list[str]:
    return _bash_array("_RUNTIME_RELOAD_SCRIPTS") + _bash_array("_RUNTIME_FRESH_SCRIPTS")


# ── the lists against the call sites ───────────────────────────────────────
# (file under src/genesis, target) -> why neither list needs it.
_UPDATER = (
    "the update routes hand update.sh to an update run that ends in a restart of "
    "the server, which the bracket records; the script itself is not server code"
)
_TIER2 = (
    "a git pathspec: the deploy-health snapshot reports what HEAD changed there "
    "since the last update.sh run; it neither runs nor reads the file"
)
_GUARDIAN = "the host guardian's own deploy list, run on the host, never by genesis-server"
EXEMPT: dict[tuple[str, str], str] = {
    ("dashboard/routes/updates.py", "scripts/update.sh"): _UPDATER,
    ("dashboard/routes/backup.py", "scripts/backup.sh"): (
        "only its existence is checked; the backup runs from its own systemd unit"
    ),
    ("restore/cli.py", "scripts/restore.sh"): (
        "the `genesis restore` command line, a separate process from the server"
    ),
    ("cc/invoker.py", "scripts/hooks/cc_span_hook.py"): (
        "registered as a PostToolUse hook for the Claude Code sessions the server "
        "launches; it runs inside those sessions, never in the server"
    ),
    ("util/pytest_lock.py", "scripts/pytest_lock_wait.py"): (
        "named in the advice a blocked pytest prints; never executed"
    ),
    ("surplus/jobs/gitnexus.py", "scripts/lib"): (
        "put on sys.path to import index_marker; every bare import of a scripts/ "
        "module is checked on its own below"
    ),
    ("observability/snapshots/deploy_health.py", "scripts/systemd"): (
        "a TIER2 pathspec, and a per-snapshot glob of the template NAMES for the "
        "missing-units report: no template content or script there is read or run"
    ),
    ("observability/snapshots/deploy_health.py", "scripts/bootstrap.sh"): _TIER2,
    ("observability/snapshots/deploy_health.py", "scripts/update.sh"): _TIER2,
    ("observability/snapshots/deploy_health.py", "scripts/hooks"): _TIER2,
    ("observability/snapshots/deploy_health.py", "scripts/lib/cc_version.sh"): _TIER2,
    ("observability/snapshots/deploy_health.py", "scripts/install_guardian.sh"): _TIER2,
    ("observability/snapshots/deploy_health.py", "scripts/guardian-gateway.sh"): _TIER2,
    ("observability/snapshots/deploy_health.py", "scripts/lib/host_swap.sh"): _TIER2,
    ("observability/snapshots/deploy_health.py", "scripts/lib/cc_tmp_volume.sh"): _TIER2,
    ("guardian/watchdog.py", "scripts/install_guardian.sh"): _GUARDIAN,
    ("guardian/watchdog.py", "scripts/guardian-gateway.sh"): _GUARDIAN,
    ("guardian/watchdog.py", "scripts/lib/host_swap.sh"): _GUARDIAN,
    ("guardian/watchdog.py", "scripts/lib/cc_tmp_volume.sh"): _GUARDIAN,
}

_SCRIPTS_TAIL = re.compile(r"(?<![A-Za-z0-9_.-])scripts/(.*)$")
_DOTTED = re.compile(r"scripts(?:\.[A-Za-z_][A-Za-z0-9_]*)+")
_BARE_PATH = re.compile(r"[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+")


def _join(names: list[str]) -> str:
    return "/".join(n for n in names if n)


def _literal_target(text: str, scripts_root: Path) -> str | None:
    """The scripts/ file a constant names, when the constant is nothing but a
    path (no whitespace). An f-string's computed part ("{}") becomes "*"."""
    text = text.strip()
    if not text or any(c.isspace() for c in text):
        return None
    m = _SCRIPTS_TAIL.search(text)
    if m:
        names = ["scripts"]
        for comp in m.group(1).split("/"):
            if "{}" in comp:
                names.append("*")
                break
            names.append(comp)
        joined = _join(names)
        return joined if joined != "scripts" else None
    if _DOTTED.fullmatch(text):
        return text.replace(".", "/") + ".py"
    # The hook launcher resolves its argument under scripts/ (genesis-hook:
    # SCRIPT_PATH="$HOOK_ROOT/scripts/$SCRIPT_NAME").
    if _BARE_PATH.fullmatch(text) and (scripts_root / text).is_file():
        return f"scripts/{text}"
    return None


def _docstrings(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _module_strings(tree: ast.Module) -> dict[str, str]:
    """Module-level NAME = "string" bindings, so a chain through a name resolves."""
    out: dict[str, str] = {}
    for stmt in tree.body:
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            out[stmt.targets[0].id] = stmt.value.value
    return out


def _part_text(part: ast.expr, names: dict[str, str]) -> str | None:
    if isinstance(part, ast.Constant) and isinstance(part.value, str):
        return part.value
    if isinstance(part, ast.Name) and part.id in names:
        return names[part.id]
    return None


def _target(parts: list[ast.expr], names: dict[str, str]) -> str | None:
    """The scripts/ path a sequence of path parts names, from the part that is
    "scripts" (or starts "scripts/") on; a part that is not a known string is
    recorded as "*"."""
    texts = [_part_text(p, names) for p in parts]
    for i, text in enumerate(texts):
        if text is None or not (text == "scripts" or text.startswith("scripts/")):
            continue
        out = [text.strip("/")]
        for later in texts[i + 1 :]:
            if later is None:
                out.append("*")
                break
            out.append(later.strip("/"))
        return _join(out)
    return None


def _scripts_modules(scripts_root: Path) -> dict[str, str]:
    """Module stem -> scripts/ path, for every .py under scripts/ whose stem is
    not also a stdlib module (an import of that name cannot be told apart)."""
    out: dict[str, str] = {}
    for py in sorted(scripts_root.rglob("*.py")):
        if py.stem not in sys.stdlib_module_names and py.stem != "genesis":
            out.setdefault(py.stem, "scripts/" + py.relative_to(scripts_root).as_posix())
    return out


def _references(src_root: Path, scripts_root: Path) -> dict[tuple[str, str], list[int]]:
    """(file relative to src_root, scripts/ target) -> lines, for the shapes above."""
    found: dict[tuple[str, str], list[int]] = {}
    modules = _scripts_modules(scripts_root)

    def add(rel: str, target: str, line: int) -> None:
        found.setdefault((rel, target), []).append(line)

    for path in sorted(src_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        rel = path.relative_to(src_root).as_posix()
        docs = _docstrings(tree)
        names = _module_strings(tree)
        inner: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
                inner.add(id(node.left))
        consumed: set[int] = set()
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.BinOp)
                and isinstance(node.op, ast.Div)
                and id(node) not in inner
            ):
                parts: list[ast.expr] = []
                cur: ast.expr = node
                while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
                    parts.append(cur.right)
                    cur = cur.left
                parts.append(cur)
                parts.reverse()
                target = _target(parts, names)
                if target:
                    consumed.update(id(p) for p in parts)
                    add(rel, target, node.lineno)
            elif isinstance(node, ast.Call):
                args: list[ast.expr] = []
                for a in node.args:
                    args.extend(a.elts if isinstance(a, (ast.List, ast.Tuple)) else [a])
                target = _target(args, names)
                if target:
                    consumed.update(id(a) for a in args)
                    add(rel, target, node.lineno)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top == "scripts" and alias.name != "scripts":
                        add(rel, alias.name.replace(".", "/") + ".py", node.lineno)
                    elif top in modules:
                        add(rel, modules[top], node.lineno)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                top = node.module.split(".")[0]
                if top == "scripts":
                    # `from scripts.lib import x`: x is a module when the file
                    # exists, else a name inside scripts/lib.py.
                    base = node.module.replace(".", "/")
                    for alias in node.names:
                        sub = f"{base}/{alias.name}.py"
                        on_disk = (scripts_root.parent / sub).is_file()
                        add(rel, sub if on_disk else base + ".py", node.lineno)
                elif top in modules:
                    add(rel, modules[top], node.lineno)
        for node in ast.walk(tree):
            if id(node) in docs or id(node) in consumed:
                continue
            if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
                value = node.value
                text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
            elif isinstance(node, ast.JoinedStr):
                text = "".join(
                    v.value if isinstance(v, ast.Constant) else "{}" for v in node.values
                )
            else:
                continue
            target = _literal_target(text, scripts_root)
            if target:
                add(rel, target, node.lineno)
    return found


def _src_refs() -> dict[tuple[str, str], list[int]]:
    return _references(REPO / "src" / "genesis", REPO / "scripts")


def _covered(target: str, listed: list[str]) -> bool:
    return any(target == p or target.startswith(p + "/") for p in listed)


def test_every_call_site_into_scripts_is_listed_or_exempt():
    listed = _listed()
    unaccounted = sorted(
        f"src/genesis/{rel}:{lines[0]} -> {target}"
        for (rel, target), lines in _src_refs().items()
        if not _covered(target, listed) and (rel, target) not in EXEMPT
    )
    assert not unaccounted, (
        "src/genesis references a scripts/ file that deploy_status.sh does not list. "
        "If genesis-server imports it, add it to _RUNTIME_RELOAD_SCRIPTS; if it runs or "
        "reads it afresh, to _RUNTIME_FRESH_SCRIPTS; otherwise add it to EXEMPT here "
        "with the reason:\n  " + "\n  ".join(unaccounted)
    )


def test_every_exemption_still_names_a_call_site():
    refs = _src_refs()
    stale = sorted(f"{rel} -> {target}" for rel, target in EXEMPT if (rel, target) not in refs)
    assert not stale, "EXEMPT rows whose call site is gone (delete them):\n  " + "\n  ".join(stale)


def _named_by_listed(listed: list[str]) -> set[str]:
    """The scripts/ files the listed scripts themselves import or source."""
    modules = _scripts_modules(REPO / "scripts")
    named: set[str] = set()
    for path in listed:
        source = (REPO / path).read_text(encoding="utf-8")
        if path.endswith(".py"):
            for node in ast.walk(ast.parse(source)):
                if isinstance(node, ast.Import):
                    tops = [a.name.split(".")[0] for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    tops = [node.module.split(".")[0]]
                else:
                    continue
                named.update(modules[t] for t in tops if t in modules)
        else:
            here = (REPO / path).parent
            for line in source.splitlines():
                if line.lstrip().startswith("#"):
                    continue
                for word in re.findall(r"[A-Za-z0-9_.-]+\.(?:sh|py)\b", line):
                    if (here / word).is_file():
                        named.add((here / word).relative_to(REPO).as_posix())
    return named


# Listed files outside scripts/, which the scan does not cover: path -> (the file
# under src/genesis that runs it, why). The test checks that file still builds
# the path as a `/`-join chain.
_OUTSIDE_SCRIPTS: dict[str, tuple[str, str]] = {
    ".claude/hooks/genesis-hook": (
        "cc/invoker.py",
        "the server runs the Bash-allowlist guard through this launcher before "
        "launching a scoped session, and reads its exit code",
    ),
}


def _chains(path: Path) -> set[str]:
    """Every `/`-join chain in a file whose parts are all string constants,
    from its first constant part on (`env.repo_root() / ".claude" / "x"` ->
    ".claude/x")."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
            continue
        parts: list[ast.expr] = []
        cur: ast.expr = node
        while isinstance(cur, ast.BinOp) and isinstance(cur.op, ast.Div):
            parts.append(cur.right)
            cur = cur.left
        parts.append(cur)
        parts.reverse()
        texts = [
            p.value if isinstance(p, ast.Constant) and isinstance(p.value, str) else None
            for p in parts
        ]
        while texts and texts[0] is None:
            texts.pop(0)
        if texts and None not in texts:
            out.add(_join([t.strip("/") for t in texts]))
    return out


def test_every_listed_script_is_consumed_and_exists():
    listed = _listed()
    assert listed, "precondition: the lists read as empty"
    # Only a call site that consumes the file counts: an exempt one (a pathspec,
    # a message) names it without the server running it.
    consuming = {t for key, _ in _src_refs().items() if key not in EXEMPT for t in [key[1]]}
    consuming |= _named_by_listed(listed)
    for path, (rel, _why) in _OUTSIDE_SCRIPTS.items():
        if path in _chains(REPO / "src" / "genesis" / rel):
            consuming.add(path)
    for path in listed:
        assert (REPO / path).is_file(), f"{path} is listed but is not a file"
        if not path.startswith("scripts/"):
            assert path in _OUTSIDE_SCRIPTS, (
                f"{path} is outside scripts/ and not in _OUTSIDE_SCRIPTS"
            )
        assert path in consuming, f"{path} is listed, but nothing the server runs names it"


def test_what_a_listed_script_uses_is_listed_too():
    """A listed script that imports or sources a scripts/ sibling makes the
    sibling server code too."""
    listed = _listed()
    missing = sorted(_named_by_listed(listed) - set(listed))
    assert not missing, f"used by a listed script but not listed: {missing}"


def test_the_lists_feed_the_right_consumers():
    runtime = _bash_array("_RUNTIME_PATHS")
    observed = _bash_array("_OBSERVED_PATHS")
    reload, fresh = _bash_array("_RUNTIME_RELOAD_SCRIPTS"), _bash_array("_RUNTIME_FRESH_SCRIPTS")
    assert runtime[:3] == ["src", "config", "pyproject.toml"], runtime
    assert set(reload) <= set(runtime), "an imported script must drive the restart decision"
    assert not set(fresh) & set(runtime), "a fresh script must not ask for a restart"
    assert set(runtime) | set(fresh) == set(observed), observed


def test_the_scan_sees_each_shape_it_claims(tmp_path):
    """Guard the guard: each shape yields its target, and prose does not."""
    scripts = tmp_path / "scripts"
    (scripts / "hooks").mkdir(parents=True)
    (scripts / "hooks" / "launched.sh").write_text("")
    (scripts / "lib").mkdir()
    (scripts / "lib" / "sibling_mod.py").write_text("")
    (scripts / "lib" / "from_mod.py").write_text("")
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "m.py").write_text(
        '"""Docstring naming scripts/in_docstring.py."""\n'
        "import os\n"
        "import sibling_mod\n"
        "from pathlib import Path\n"
        'SCRIPTS = "scripts"\n'
        'a = Path("r") / "scripts" / "lib" / "joined.py"\n'
        'b = Path("r") / "scripts" / name\n'
        'c = os.path.join(root, "scripts", "os_join.sh")\n'
        'd = "scripts/literal.sh"\n'
        'e = f"{ref}:scripts/fstring.sh"\n'
        'f = b"scripts/bytes.sh"\n'
        'g = "run scripts/in_prose.sh to fix it"\n'
        'h = f"{root}/scripts/{name}"\n'
        'i = "/hooks/", "/scripts/"\n'
        'j = Path("r") / "scripts/lib/slashed.py"\n'
        'k = Path("r") / f"scripts/{name}"\n'
        'l = "/".join([root, "scripts", "listed.py"])\n'
        'm = Path("r") / SCRIPTS / "named.py"\n'
        'n = ["python", "-m", "scripts.modform"]\n'
        'o = [launcher, "hooks/launched.sh"]\n'
        'p = "hooks/not_a_file.sh"\n'
        "import scripts.hooks.pkgform\n"
        "from scripts.lib import from_mod\n"
        "from scripts.lib.helpers import some_name\n"
    )
    refs = {t for _, t in _references(pkg, scripts)}
    assert refs == {
        "scripts/lib/joined.py",
        "scripts/*",
        "scripts/os_join.sh",
        "scripts/literal.sh",
        "scripts/fstring.sh",
        "scripts/bytes.sh",
        "scripts/lib/slashed.py",
        "scripts/listed.py",
        "scripts/named.py",
        "scripts/modform.py",
        "scripts/hooks/launched.sh",
        "scripts/lib/sibling_mod.py",
        "scripts/hooks/pkgform.py",
        "scripts/lib/from_mod.py",
        "scripts/lib/helpers.py",
    }, refs


# ── behaviour, through the deploy script ────────────────────────────────────
# Named here so a parametrization cannot come up empty (an empty list would
# collect no case and read as a pass); a test keeps them equal to the lists.
_RELOAD = ("scripts/lib/index_marker.py", "scripts/hooks/worktree_claim.py")
_FRESH = (
    "scripts/disk_reclaim.py",
    "scripts/hooks/bash_allowlist_guard.sh",
    "scripts/hooks/bash_allowlist_lib.sh",
    ".claude/hooks/genesis-hook",
)


def test_the_behaviour_cases_are_the_lists():
    assert set(_bash_array("_RUNTIME_RELOAD_SCRIPTS")) == set(_RELOAD)
    assert set(_bash_array("_RUNTIME_FRESH_SCRIPTS")) == set(_FRESH)


def _later(st, seconds: int = 60) -> str:
    return f"@{st['booted_at'] + seconds} +0000"


def _env(st, **extra) -> dict:
    return {**st["env"], **extra}


def _bracket(st) -> str:
    r = _run(st, "status")
    assert r.returncode == 0, r.stderr
    for line in r.stdout.splitlines():
        if line.startswith("bracket: "):
            return line.removeprefix("bracket: ")
    raise AssertionError(r.stdout)


def _verify(st, token: str) -> bool:
    r = _run(st, "status", "--verify", token)
    assert r.returncode in (0, 1), (r.returncode, r.stdout, r.stderr)
    return r.returncode == 0


def _seed(st, path: str) -> None:
    """Put a first version of *path* in the tree and have the server boot on it,
    so the case that follows changes the file's CONTENT. The station's first
    commit holds none of the listed scripts: without this every case would be a
    file appearing, which a fingerprint of names alone would also catch."""
    _advance_upstream(st, "seed", {path: "# v1\n"})
    r = _run(st, "pull", env=_env(st, GIT_COMMITTER_DATE=_later(st, 60)))
    assert r.returncode == 0, r.stderr
    # The unit's start moves past that pull: the server booted from it, as
    # after a restart, without a restart in the systemctl log.
    st["env"]["BOOTED_AT"] = str(st["booted_at"] + 120)
    assert (st["root"] / path).read_text() == "# v1\n", "precondition: v1 is in the tree"


def _change(st, path: str):
    """Seed *path*, take a bracket, then pull a new version of it."""
    _seed(st, path)
    start = _bracket(st)
    assert start.startswith("b1-"), f"precondition: a bracket to void, got {start}"
    _advance_upstream(st, "script", {path: "# v2\n"})
    r = _run(st, "pull", env=_env(st, GIT_COMMITTER_DATE=_later(st, 180)))
    assert r.returncode == 0, r.stderr
    return start, r


@pytest.mark.parametrize("path", _RELOAD)
def test_a_pull_of_an_imported_script_voids_the_bracket_and_is_pending(station, path):
    start, r = _change(station, path)
    assert not _verify(station, start), f"a pull of {path} left the bracket valid"
    assert "PENDING" in r.stdout and path in r.stdout, r.stdout


@pytest.mark.parametrize("path", _FRESH)
def test_a_pull_of_a_script_run_afresh_voids_the_bracket_but_is_not_pending(station, path):
    start, r = _change(station, path)
    assert not _verify(station, start), f"a pull of {path} left the bracket valid"
    assert "PENDING" not in r.stdout, "a script run afresh needs no restart"
    after = _bracket(station)
    assert after.startswith("b1-"), f"a new bracket can be taken at once, got {after}"
    assert _verify(station, after)


@pytest.mark.parametrize("path", _FRESH)
def test_an_uncommitted_edit_to_a_script_run_afresh_is_a_runtime_edit(station, path):
    _seed(station, path)
    assert _bracket(station).startswith("b1-"), "precondition: clean before the edit"
    (station["root"] / path).write_text("# edited in place\n")
    b = _bracket(station)
    assert b.startswith("unknown (uncommitted runtime edits"), b


def test_status_from_a_worktree_uses_the_main_checkouts_lists(station, tmp_path):
    """Round-1 review (Codex P1): `status` from a linked worktree reports the main
    checkout, but it sourced the WORKTREE's lib/deploy_status.sh, so the lists that
    decide what the bracket hashes came from a branch that can be older than main.
    Here the main checkout carries this script and its libs, and a worktree's copy
    of the lists lacks scripts/disk_reclaim.py. A pull of that script into the main
    checkout must still void a bracket taken from the worktree: status hands over to
    the main checkout's own script, whose lists name it."""
    lib = REPO / "scripts" / "lib"
    files = {
        f"scripts/lib/{f.name}": f.read_text()
        for f in sorted(lib.iterdir())
        if f.is_file() and f.suffix in {".sh", ".py"}
    }
    files["scripts/deploy_code_only.sh"] = (REPO / "scripts" / "deploy_code_only.sh").read_text()
    files["scripts/disk_reclaim.py"] = "# v1\n"
    _advance_upstream(station, "the deploy script, as a real checkout carries it", files)
    r = _run(station, "pull", env=_env(station, GIT_COMMITTER_DATE=_later(station, 60)))
    assert r.returncode == 0, r.stderr
    station["env"]["BOOTED_AT"] = str(station["booted_at"] + 120)

    wt = tmp_path / "elsewhere" / "wt"
    _git(station["root"], "worktree", "add", "-q", "-b", "wt", str(wt))
    stale = wt / "scripts" / "lib" / "deploy_status.sh"
    entry = "    scripts/disk_reclaim.py"
    text = stale.read_text()
    assert text.count(entry) == 1, "precondition: the entry this case removes"
    stale.write_text(text.replace(entry, "    # (not yet listed on this branch)"))
    from_wt = ["bash", str(wt / "scripts" / "deploy_code_only.sh"), "status"]
    # The systemctl shim names the unit's WorkingDirectory from UNIT_DIR, else from
    # GENESIS_DEPLOY_ROOT; the hand-over drops the latter, so pin the unit where a real
    # one runs: the main checkout.
    wt_env = _env(station, GENESIS_DEPLOY_ROOT=str(wt), UNIT_DIR=str(station["root"]))

    r = subprocess.run(from_wt, env=wt_env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    assert "reporting the main checkout" in r.stdout, r.stdout
    start = next(
        (
            ln.removeprefix("bracket: ")
            for ln in r.stdout.splitlines()
            if ln.startswith("bracket: ")
        ),
        "",
    )
    assert start.startswith("b1-"), r.stdout

    _advance_upstream(station, "script", {"scripts/disk_reclaim.py": "# v2\n"})
    r = _run(station, "pull", env=_env(station, GIT_COMMITTER_DATE=_later(station, 180)))
    assert r.returncode == 0, r.stderr

    r = subprocess.run(
        [*from_wt, "--verify", start], env=wt_env, capture_output=True, text=True, timeout=120
    )
    assert r.returncode == 1, (
        "a bracket taken from a worktree whose lists lag main stayed valid across a pull "
        f"of a script main lists:\n{r.stdout}\n{r.stderr}"
    )


def test_a_pull_of_an_unlisted_script_leaves_the_bracket_valid(station):
    """The control: scripts/ as a whole is not server code."""
    start, _ = _change(station, "scripts/some_tool.py")
    assert _verify(station, start)


def test_a_deploy_of_an_imported_script_restarts(station):
    """index_marker is imported once and kept, so a new copy needs a restart."""
    _seed(station, _RELOAD[0])
    tip = _advance_upstream(station, "script", {_RELOAD[0]: "# v2\n"})
    r = _run(station, env=_env(station, GIT_COMMITTER_DATE=_later(station, 180)))
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert _git(station["root"], "rev-parse", "HEAD") == tip
    assert "Nothing to deploy" not in r.stdout, r.stdout
    assert _restarted(station), r.stdout


def test_a_deploy_of_a_script_run_afresh_does_not_restart(station):
    """disk_reclaim runs as a new process each time: a restart would buy nothing."""
    _seed(station, _FRESH[0])
    tip = _advance_upstream(station, "script", {_FRESH[0]: "# v2\n"})
    r = _run(station, env=_env(station, GIT_COMMITTER_DATE=_later(station, 180)))
    assert r.returncode == 0, (r.stdout, r.stderr)
    assert _git(station["root"], "rev-parse", "HEAD") == tip
    assert "Nothing to deploy" in r.stdout, r.stdout
    assert not _restarted(station), r.stdout
