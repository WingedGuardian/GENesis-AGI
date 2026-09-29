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
    parsed requirements (name, extras, specifier, URL and marker), not strings.

Not compared: other pyproject fields that also need a reinstall (entry points,
package discovery). None exist today; a change to one needs update.sh.

Exit codes:
  0  editable from this checkout, with these requirements and requires-python;
  1  not (each difference is printed);
  2  cannot tell (unreadable input or metadata, the project not installed,
     `packaging` unavailable). Callers treat 2 exactly like 1 and refuse.

Why equality and not "is it satisfied": a code-only deploy never reinstalls, so
it is safe only while the install still describes this tree. `scripts/update.sh`
reinstalls unconditionally, which rewrites this metadata, so every refusal here
is one that a successful update.sh clears. A satisfaction check guessed which
optional groups an install uses, and could refuse a deploy update.sh then could
not fix.
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit


class _CannotTell(Exception):
    """A question the install cannot answer: exit 2 (refuse)."""


def _differences(root: Path, pyproject: str) -> list[str]:
    import json
    from importlib.metadata import PackageNotFoundError, distribution

    from packaging.markers import Marker
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.specifiers import SpecifierSet
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
        want_python = str(SpecifierSet(project.get("requires-python", "")))
    except (
        tomllib.TOMLDecodeError,
        KeyError,
        TypeError,
        AttributeError,
        InvalidRequirement,
    ) as exc:
        raise _CannotTell(f"cannot read the incoming pyproject.toml: {exc}") from exc

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
    for line in diffs:
        print(line)
    return 1 if diffs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
