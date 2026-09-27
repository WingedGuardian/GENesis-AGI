"""Does the running interpreter's environment satisfy a pyproject.toml's dependencies?

Reads a pyproject.toml from stdin. Run it with the venv's own python: the answer
is about what THAT interpreter can import.

Exit codes:
  0  every [project].dependencies requirement whose marker applies here is
     installed at a satisfying version;
  1  at least one is missing or at the wrong version (each is printed);
  2  cannot tell (unparseable input, `packaging` unavailable). Callers treat 2
     exactly like 1 and refuse: an unanswered question is not a yes.

Why this exists: a code-only deploy restarts the server without reinstalling,
which is safe only while the venv still satisfies the project's dependencies.
Diffing pyproject.toml is both too broad (a comment change refuses) and
bypassable (a dependency change merged earlier by some other path passes).
Asking the environment directly answers the question the deploy needs.

Also checked: [project].requires-python against the running interpreter. A
direct-URL requirement (`pkg @ https://...`) cannot be verified from installed
metadata, so it answers 2 (cannot tell) rather than passing.

Limits, stated so they are not mistaken for coverage: optional-dependency
groups are not checked, and an extra (`pkg[extra]>=1`) is checked for `pkg`
alone, not for the extra's own requirements.
"""

from __future__ import annotations

import sys
import tomllib


def main() -> int:
    try:
        from importlib.metadata import PackageNotFoundError, version

        from packaging.requirements import InvalidRequirement, Requirement
        from packaging.specifiers import InvalidSpecifier, SpecifierSet
    except ImportError as exc:  # pragma: no cover - environment-dependent
        print(f"cannot check dependencies: {exc}", file=sys.stderr)
        return 2

    try:
        data = tomllib.loads(sys.stdin.read())
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        print(f"cannot parse pyproject.toml: {exc}", file=sys.stderr)
        return 2

    deps = data.get("project", {}).get("dependencies")
    if not isinstance(deps, list) or not all(isinstance(d, str) for d in deps):
        print("pyproject.toml has no readable [project].dependencies list", file=sys.stderr)
        return 2

    unmet: list[str] = []
    requires_python = data.get("project", {}).get("requires-python")
    if requires_python is not None:
        try:
            wanted = SpecifierSet(str(requires_python))
        except InvalidSpecifier as exc:
            print(f"cannot parse requires-python {requires_python!r}: {exc}", file=sys.stderr)
            return 2
        running = ".".join(str(n) for n in sys.version_info[:3])
        if not wanted.contains(running, prereleases=True):
            unmet.append(f"python: running {running}, wants {wanted}")

    for spec in deps:
        try:
            req = Requirement(spec)
        except InvalidRequirement as exc:
            print(f"cannot parse requirement {spec!r}: {exc}", file=sys.stderr)
            return 2
        if req.marker is not None and not req.marker.evaluate():
            continue
        if req.url:
            print(
                f"cannot verify direct-URL requirement {spec!r} from installed metadata",
                file=sys.stderr,
            )
            return 2
        try:
            installed = version(req.name)
        except PackageNotFoundError:
            unmet.append(f"{req.name}: not installed (wants {req.specifier or 'any version'})")
            continue
        # prereleases=True: the question is whether what IS installed satisfies
        # the range, not which release a resolver would pick.
        if req.specifier and not req.specifier.contains(installed, prereleases=True):
            unmet.append(f"{req.name}: installed {installed}, wants {req.specifier}")

    for line in unmet:
        print(line)
    return 1 if unmet else 0


if __name__ == "__main__":
    sys.exit(main())
