"""Was the running venv installed from THIS checkout, with THIS pyproject.toml's
requirements?

Usage: venv_matches_pyproject.py <checkout-root>   (the pyproject.toml on stdin)

Run it with the venv's own python: the answer is about what that interpreter
imports. It asks the installed project's own metadata, never the environment's
guesswork:

  - the project is installed EDITABLE, from <checkout-root> (its
    direct_url.json), so the restarted server imports this checkout's src/ and
    not a worktree's or a wheel's copy;
  - the installed Requires-Python and requirements (base and every optional
    group) EQUAL the ones in the pyproject.toml read from stdin, compared as
    parsed requirements (name, extras, specifier, URL and marker), not strings;
  - every BASE requirement whose marker applies to this interpreter is actually
    installed, at a version its specifier accepts (pre-releases allowed);
  - the installed entry points EQUAL the pyproject's [project.scripts],
    [project.gui-scripts] and [project.entry-points.*]: a console command or a
    plugin gets its shim or its entry_points.txt line only from a reinstall;
  - this interpreter satisfies the incoming requires-python;
  - the package root the install's editable .pth names EQUALS the one the
    incoming pyproject lays out (below).

The package root. The editable install is a path entry: setuptools writes the
`packages.find` directory as the one line of an `__editable__.*.pth` listed in
the install's RECORD (measured, setuptools 84, the version the live install
records), so a package added under that directory imports without a reinstall,
and a pyproject that MOVES it does not import until a reinstall rewrites the
.pth. The gate compares that recorded line with the incoming root. It reads the
incoming root from one shape only, the one it was measured on: [build-system]
build-backend "setuptools.build_meta" with no backend-path, and [tool.setuptools]
holding nothing but `packages.find` with exactly one `where` (include, exclude and
namespaces select packages under that root and leave the .pth unchanged,
measured), and that `where` must not be the checkout itself: setuptools installs
"." or "./" through a finder, not a path line (measured). Any other pyproject
build configuration (no [tool.setuptools], package-dir, an explicit package list,
two roots, another backend, an unknown key) is exit 2: the gate would have to
model setuptools to predict the layout, so a code-only deploy of such a tree goes
to update.sh, after the reinstall too, until this gate learns that shape. The
gate reads only the pyproject.toml it is given: a setup.cfg or setup.py beside it
could also move the layout, and is not seen (the repository has neither). An
install whose .pth imports a finder (setuptools' form for layouts it cannot
express as one path) or that records no editable .pth names no root, which is a
difference a reinstall of the supported shape clears (it writes the plain .pth,
measured). The .pth's directory is compared as recorded, whether or not it exists
on disk: the incoming tree is not checked out yet when the deploy asks.

Not compared: the project's own version: nothing reads the installed version (the
version gate reads pyproject.toml), and equality of the requirements already
covers a bump that changes them.

Exit codes:
  0  editable from this checkout, with these requirements and requires-python,
     the base requirements present, and the .pth naming the incoming package root;
  1  not (each difference is printed); a reinstall clears every one of them;
  2  cannot tell (unreadable input or metadata, the project not installed,
     `packaging` unavailable, or a build configuration outside the one shape above,
     in which case the other differences are still printed). Callers treat 2
     exactly like 1 and refuse;
  3  this interpreter does not satisfy the incoming requires-python. A reinstall
     into this venv cannot clear that, so callers must not send it to update.sh.

Why equality AND presence. Equality is about provenance: a code-only deploy never
reinstalls, so it is safe only while the install still describes this tree, and
`scripts/update.sh` reinstalls unconditionally, which rewrites this metadata.
Its cost is that a requirements change the venv already satisfies is still
sent to a reinstall. Equality alone cannot see a package removed or downgraded
after the install, so the base requirements are also checked for presence.
Optional groups are not: which of them an install uses is not recorded
anywhere, and `update.sh` installs none. A base requirement that names extras
is checked by its own version only, as pip's resolver would have left it.
"""

from __future__ import annotations

import os
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit


class _CannotTell(Exception):
    """A question the install cannot answer: exit 2 (refuse)."""


class _WrongInterpreter(Exception):
    """This python cannot satisfy the incoming requires-python: exit 3."""


_FIND_KEYS = {"where", "include", "exclude", "namespaces"}


def _incoming_root(doc: dict, root: Path) -> Path:
    """The package root the incoming pyproject lays out, read from the one build
    configuration this gate can certify (module docstring); _CannotTell otherwise."""
    shape = (
        "build configuration outside what this gate can compare ([build-system] "
        'build-backend = "setuptools.build_meta", and [tool.setuptools] holding only '
        "packages.find with one `where`): it cannot tell whether the installed "
        "package root is the one this tree lays out, so a code-only deploy of it is refused"
    )
    build = doc.get("build-system")
    if (
        not isinstance(build, dict)
        or build.get("build-backend") != "setuptools.build_meta"
        or "backend-path" in build
    ):
        raise _CannotTell(f"incoming pyproject.toml: {shape} ([build-system])")
    tool = doc.get("tool")
    setuptools = tool.get("setuptools") if isinstance(tool, dict) else None
    packages = setuptools.get("packages") if isinstance(setuptools, dict) else None
    find = packages.get("find") if isinstance(packages, dict) else None
    if (
        not isinstance(setuptools, dict)
        or set(setuptools) != {"packages"}
        or not isinstance(packages, dict)
        or set(packages) != {"find"}
        or not isinstance(find, dict)
        or not set(find) <= _FIND_KEYS
    ):
        raise _CannotTell(f"incoming pyproject.toml: {shape} ([tool.setuptools])")
    where = find.get("where")
    if not (isinstance(where, list) and len(where) == 1 and isinstance(where[0], str) and where[0]):
        raise _CannotTell(f"incoming pyproject.toml: {shape} (packages.find.where = {where!r})")
    # The incoming tree is not checked out yet when a deploy asks, so nothing below
    # the checkout root may be looked up on disk: a symlink there belongs to the OLD
    # tree, and following it can name the installed root while the incoming commit
    # lays out another (round-1 review). Only the root itself is resolved (it is the
    # same directory before and after the merge); `where` is joined lexically, the
    # way site.py joins a .pth line, and _installed_roots reads the .pth the same way.
    try:
        base = root.resolve()
    except (OSError, ValueError) as exc:
        raise _CannotTell(f"cannot resolve the checkout root {root}: {exc}") from exc
    want = Path(os.path.normpath(base / where[0]))
    # A `where` that is the checkout itself ("." or "./") is installed through a
    # finder, not a path line (measured, setuptools 84), so a reinstall would
    # never produce the .pth this gate compares.
    if want == base:
        raise _CannotTell(
            f"incoming pyproject.toml: {shape} (packages.find.where names the checkout "
            "itself, which setuptools installs through a finder)"
        )
    return want


def _installed_roots(dist, root: Path) -> tuple[set[Path], str]:
    """The directories the install's editable .pth files put on sys.path, and a
    reason when they name no root (a finder, or no editable .pth recorded).

    Each line is joined and normalised LEXICALLY, as site.addpackage does
    (os.path.join + os.path.abspath), never resolved: a symlink inside the checkout
    belongs to the tree as it stands, and the incoming root is compared by name
    (_incoming_root). A line that names the checkout through the path the caller
    gave is rebased onto the resolved checkout root, so a checkout reached through a
    symlinked parent still compares equal to itself."""
    given = os.path.normpath(os.path.abspath(root))
    try:
        base = str(root.resolve())
    except (OSError, ValueError) as exc:
        raise _CannotTell(f"cannot resolve the checkout root {root}: {exc}") from exc
    editable = [
        f for f in dist.files or [] if f.name.startswith("__editable__") and f.name.endswith(".pth")
    ]
    if not editable:
        return set(), "the install records no editable .pth"
    roots: set[Path] = set()
    for f in editable:
        pth = Path(dist.locate_file(f))
        try:
            text = pth.read_text()
        except (OSError, ValueError) as exc:
            raise _CannotTell(f"cannot read the install's {pth}: {exc}") from exc
        # Line by line as site.addpackage reads it: a comment or blank line is
        # skipped, a line starting "import " / "import\t" is executed, anything
        # else is a directory relative to the .pth's own, right-stripped only.
        for line in text.splitlines():
            if line.startswith("#") or not line.strip():
                continue
            # setuptools writes an import line to load a finder, which maps
            # packages rather than naming a root.
            if line.startswith(("import ", "import\t")):
                return set(), f"the install's {pth.name} loads a finder, not a package root"
            try:
                named = os.path.normpath(os.path.join(pth.parent, line.rstrip()))
            except (TypeError, ValueError) as exc:
                raise _CannotTell(f"cannot read {line!r} in the install's {pth}: {exc}") from exc
            if given != base and (named == given or named.startswith(given + os.sep)):
                named = base + named[len(given) :]
            roots.add(Path(named))
    return roots, ""


def _differences(root: Path, pyproject: str) -> tuple[list[str], str]:
    """The differences a reinstall clears, and (non-empty) why the package root
    cannot be compared at all."""
    import json
    import platform
    from importlib.metadata import PackageNotFoundError, distribution
    from importlib.metadata import version as installed_version

    from packaging.markers import Marker
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.specifiers import InvalidSpecifier, SpecifierSet
    from packaging.utils import canonicalize_name

    def key(req: Requirement) -> tuple:
        return (
            canonicalize_name(req.name),
            tuple(sorted(canonicalize_name(e) for e in req.extras)),
            str(req.specifier),
            req.url or "",
            str(req.marker) if req.marker else "",
        )

    try:
        doc = tomllib.loads(pyproject)
        project = doc["project"]
        name = project["name"]
        want = {key(Requirement(s)) for s in project.get("dependencies", [])}
        for group, specs in (project.get("optional-dependencies") or {}).items():
            for spec in specs:
                req = Requirement(spec)
                # An optional group's requirement is recorded with its extra
                # folded into the marker; build the same shape to compare.
                extra = f'extra == "{canonicalize_name(group)}"'
                req.marker = Marker(f"({req.marker}) and {extra}" if req.marker else extra)
                want.add(key(req))
        want_python_set = SpecifierSet(project.get("requires-python", ""))
        want_python = str(want_python_set)
        base = [Requirement(s) for s in project.get("dependencies", [])]
        # (group, name, target) as entry_points.txt records them.
        tables = [
            ("console_scripts", project.get("scripts") or {}),
            ("gui_scripts", project.get("gui-scripts") or {}),
            *(project.get("entry-points") or {}).items(),
        ]
        want_eps = {
            (group, name, str(target).strip())
            for group, table in tables
            for name, target in table.items()
        }
    except (
        tomllib.TOMLDecodeError,
        KeyError,
        TypeError,
        AttributeError,
        InvalidRequirement,
        InvalidSpecifier,
    ) as exc:
        raise _CannotTell(f"cannot read the incoming pyproject.toml: {exc}") from exc

    running = platform.python_version()
    if not want_python_set.contains(running, prereleases=True):
        raise _WrongInterpreter(
            f"this venv's python is {running}, and the incoming pyproject.toml "
            f"requires-python is {want_python!r}"
        )
    # An uncertifiable layout is exit 2, but the other differences are still
    # collected and printed: the report says everything that is true.
    try:
        want_root: Path | None = _incoming_root(doc, root)
        layout_unknown = ""
    except _CannotTell as exc:
        want_root, layout_unknown = None, str(exc)

    try:
        dist = distribution(name)
    except PackageNotFoundError as exc:
        raise _CannotTell(f"{name} is not installed in this venv") from exc

    diffs: list[str] = []
    try:
        direct = json.loads(dist.read_text("direct_url.json") or "{}")
    except json.JSONDecodeError:
        direct = {}
    if not isinstance(direct, dict) or not isinstance(direct.get("dir_info", {}), dict):
        raise _CannotTell(f"{name}'s direct_url.json is not the documented shape")
    url = direct.get("url", "")
    installed_from = unquote(urlsplit(url).path) if url.startswith("file://") else ""
    if not direct.get("dir_info", {}).get("editable"):
        diffs.append(
            f"{name} is not an editable install (installed from {url or 'an unknown source'})"
        )
    elif not installed_from or Path(installed_from).resolve() != root.resolve():
        diffs.append(f"{name} is installed from {installed_from or url}, not {root}")
    if want_root is not None and direct.get("dir_info", {}).get("editable"):
        have_roots, no_root = _installed_roots(dist, root)
        if no_root:
            diffs.append(
                f"package root: {no_root}, and the incoming pyproject lays out {want_root}"
            )
        elif have_roots != {want_root}:
            listed = ", ".join(sorted(str(p) for p in have_roots))
            diffs.append(
                f"package root: the install imports from {listed}, "
                f"and the incoming pyproject lays out {want_root}"
            )

    have_python = str(SpecifierSet(dist.metadata.get("Requires-Python") or ""))
    if have_python != want_python:
        diffs.append(f"requires-python: installed {have_python!r}, pyproject {want_python!r}")
    try:
        have = {key(Requirement(s)) for s in dist.requires or []}
    except InvalidRequirement as exc:
        raise _CannotTell(f"cannot parse {name}'s installed requirements: {exc}") from exc
    for k in sorted(want - have):
        diffs.append(f"not installed: {k[0]} {k[2]} {k[4]}".rstrip())
    for k in sorted(have - want):
        diffs.append(f"installed but no longer required: {k[0]} {k[2]} {k[4]}".rstrip())
    have_eps = {(ep.group, ep.name, ep.value.strip()) for ep in dist.entry_points}
    for group, ep_name, target in sorted(want_eps - have_eps):
        diffs.append(f"entry point not installed: [{group}] {ep_name} = {target}")
    for group, ep_name, target in sorted(have_eps - want_eps):
        diffs.append(
            f"entry point installed but no longer declared: [{group}] {ep_name} = {target}"
        )

    # Presence: equal metadata says the install DESCRIBES this tree, not that
    # what it installed is still there.
    for req in base:
        if req.marker and not req.marker.evaluate({"extra": ""}):
            continue
        try:
            got = installed_version(req.name)
        except PackageNotFoundError:
            diffs.append(f"missing: {req.name} (required {req.specifier or 'at any version'})")
            continue
        if req.specifier and not req.specifier.contains(got, prereleases=True):
            diffs.append(f"unsatisfied: {req.name} {got} does not meet {req.specifier}")
    return diffs, layout_unknown


def main(argv: list[str]) -> int:
    try:
        if len(argv) != 2:
            raise _CannotTell("usage: venv_matches_pyproject.py <checkout-root>")
        diffs, layout_unknown = _differences(Path(argv[1]), sys.stdin.read())
    except ImportError as exc:
        print(f"cannot tell: {exc}", file=sys.stderr)
        return 2
    except _CannotTell as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except _WrongInterpreter as exc:
        print(str(exc))
        return 3
    for line in diffs:
        print(line)
    if layout_unknown:
        print(layout_unknown, file=sys.stderr)
        return 2
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
