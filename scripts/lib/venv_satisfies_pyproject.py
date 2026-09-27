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

Optional-dependency groups are checked when this install USES them — decided
by the environment: a group with one of its OWN packages installed (one the
base dependencies do not already pull in, transitively) is in use, and then all
of its requirements must be satisfied. Other groups are skipped.

Limits, stated so they are not mistaken for coverage: a group whose packages
all arrive through the base dependencies cannot be told from an unused one and
is skipped; a group this install uses but whose own packages were uninstalled
reads as unused; and an extra on a requirement (`pkg[extra]>=1`) is checked for
`pkg` alone, not for the extra's own requirements.
"""

from __future__ import annotations

import sys
import tomllib


class _CannotTell(Exception):
    """A requirement the environment cannot be asked about: exit 2 (refuse)."""


def canonicalize_name(name: str) -> str:
    """PEP 503 name normalisation (as packaging.utils.canonicalize_name)."""
    import re

    return re.sub(r"[-_.]+", "-", name).lower()


def _base_closure(base) -> set[str]:
    """Canonical names of every INSTALLED distribution the base requirements pull
    in, transitively, read from installed metadata. A dependency's own extras are
    followed only where a requirement asks for them."""
    from importlib.metadata import PackageNotFoundError, requires

    from packaging.requirements import InvalidRequirement, Requirement

    seen: set[str] = set()
    stack = [(canonicalize_name(r.name), frozenset(r.extras)) for r in base]
    while stack:
        name, extras = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        try:
            specs = requires(name) or []
        except PackageNotFoundError:
            continue
        for spec in specs:
            try:
                req = Requirement(spec)
            except InvalidRequirement:
                continue
            if req.marker is not None:
                envs = [{"extra": e} for e in ("", *sorted(extras))]
                try:
                    if not any(req.marker.evaluate(env) for env in envs):
                        continue
                except Exception:  # noqa: BLE001 — an unevaluable marker adds nothing
                    continue
            stack.append((canonicalize_name(req.name), frozenset(req.extras)))
    return seen


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

    def parse(spec: str):
        """The Requirement, or None when its marker does not apply here. Raises
        _CannotTell for a requirement installed metadata cannot verify."""
        try:
            req = Requirement(spec)
        except InvalidRequirement as exc:
            raise _CannotTell(f"cannot parse requirement {spec!r}: {exc}") from exc
        if req.marker is not None and not req.marker.evaluate():
            return None
        if req.url:
            raise _CannotTell(
                f"cannot verify direct-URL requirement {spec!r} from installed metadata"
            )
        return req

    def installed_version(req) -> str | None:
        try:
            return version(req.name)
        except PackageNotFoundError:
            return None

    def check(req, where: str = "") -> None:
        installed = installed_version(req)
        if installed is None:
            unmet.append(
                f"{req.name}{where}: not installed (wants {req.specifier or 'any version'})"
            )
            return
        # prereleases=True: the question is whether what IS installed satisfies
        # the range, not which release a resolver would pick.
        if req.specifier and not req.specifier.contains(installed, prereleases=True):
            unmet.append(f"{req.name}{where}: installed {installed}, wants {req.specifier}")

    try:
        base: list = []
        for spec in deps:
            req = parse(spec)
            if req is not None:
                base.append(req)
                check(req)

        # Optional-dependency groups this install USES. Which extras were chosen
        # at install time is not recorded anywhere reliable, so "in use" is
        # decided by the environment — but only by a package the BASE
        # dependencies do not already pull in. A package that is in the base
        # closure (openai arrives through litellm, for example) is installed on
        # every install and says nothing about the extra; counting it marked
        # such a group "in use" everywhere and refused deploys the full update
        # cannot fix, since it installs no extras either. A group with one of
        # its OWN packages installed must then be satisfied in full.
        closure = _base_closure(base)
        optional = data.get("project", {}).get("optional-dependencies") or {}
        if not isinstance(optional, dict):
            raise _CannotTell("pyproject.toml has an unreadable [project.optional-dependencies]")
        for group, specs in sorted(optional.items()):
            if not isinstance(specs, list) or not all(isinstance(s, str) for s in specs):
                raise _CannotTell(f"optional-dependency group {group!r} is not a list of strings")
            reqs = [r for r in (parse(s) for s in specs) if r is not None]
            own = [r for r in reqs if canonicalize_name(r.name) not in closure]
            if any(installed_version(r) is not None for r in own):
                for r in reqs:
                    check(r, f" (extra '{group}')")
    except _CannotTell as exc:
        print(str(exc), file=sys.stderr)
        return 2

    for line in unmet:
        print(line)
    return 1 if unmet else 0


if __name__ == "__main__":
    sys.exit(main())
