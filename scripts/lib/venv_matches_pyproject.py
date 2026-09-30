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
  - this interpreter satisfies the incoming requires-python.

Not compared: package discovery. The editable install is a path entry to the
source directory (a .pth, measured on a live install), so a package added under
it imports without a reinstall; moving that directory would need update.sh. Nor
the project's own version: nothing reads the installed version (the version gate
reads pyproject.toml), and equality of the requirements already covers a bump
that changes them.

Exit codes:
  0  editable from this checkout, with these requirements and requires-python,
     and the base requirements are present;
  1  not (each difference is printed); a reinstall clears every one of them;
  2  cannot tell (unreadable input or metadata, the project not installed,
     `packaging` unavailable). Callers treat 2 exactly like 1 and refuse;
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

import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit


class _CannotTell(Exception):
    """A question the install cannot answer: exit 2 (refuse)."""


class _WrongInterpreter(Exception):
    """This python cannot satisfy the incoming requires-python: exit 3."""


def _differences(root: Path, pyproject: str) -> list[str]:
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
        project = tomllib.loads(pyproject)["project"]
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
    return diffs


def main(argv: list[str]) -> int:
    try:
        if len(argv) != 2:
            raise _CannotTell("usage: venv_matches_pyproject.py <checkout-root>")
        diffs = _differences(Path(argv[1]), sys.stdin.read())
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
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
