"""Extension seams: Protocol/ABC bases and the classes that implement them.

Computed at query time from the code index. A class implements a seam
``nominal``-ly when one of its bases names the seam, and ``structural``-ly when
it has a call-compatible public method for every public method the seam
declares or inherits from a parent seam. Call-compatible is checked on: same
async-ness, no more required arguments than the seam, room for each of the
seam's positional arguments, and the seam's keyword-only names accepted (or
``**kwargs``). Parameter names of positional arguments, the seam's own
``*args``/``**kwargs`` and annotations are not checked. A parent seam whose name
is defined more than once cannot be resolved: nothing is inherited through it,
and the entry carries ``contract_ambiguous``. A single-method seam matches on
one name, so its structural links are ``low`` confidence, as is a nominal link
to a class name defined in more than one module. A Protocol that subclasses a
seam is listed as ``extends``, and is never a structural implementer.

The index records methods, not attributes. A class that supplies a seam member
as a class attribute (``name = "x"`` for a ``name`` property) cannot match, so
a class that matches everything except such sync, self-only members is listed
under ``possible`` with what it lacks. The index records no decorators either,
so a member that is really a zero-argument method looks the same, and a class
simply missing that method can land in ``possible`` too: it is a list to check,
not a list of matches. Methods a class inherits from a concrete parent are not
in the index, so such an implementer is not found.
"""

from __future__ import annotations

import ast
from typing import NamedTuple

import aiosqlite

#: Registry-style extension points: (kind, where an instance is added, how).
REGISTRIES: tuple[tuple[str, str, str], ...] = (
    ("MCP tool", "src/genesis/mcp/health/", "a module, imported in its __init__.py"),
    ("background session profile", "src/genesis/cc/direct_session.py", "an entry in PROFILES"),
    ("capability module", "config/modules/", "a YAML from example.yaml.template"),
    ("channel", "src/genesis/channels/", "wired in src/genesis/runtime/init/outreach.py"),
    ("skill", ".claude/skills/", "<name>/SKILL.md, or under src/genesis/skills/"),
    (
        "tool provider",
        "src/genesis/providers/",
        "registered in src/genesis/runtime/init/providers.py",
    ),
    ("hosting adapter", "src/genesis/hosting/", "implements HostFrameworkAdapter (types.py)"),
    ("surplus executor", "src/genesis/runtime/init/surplus.py", "wired to the surplus scheduler"),
    (
        "signal collector",
        "src/genesis/learning/signals/",
        "swapped in by src/genesis/runtime/init/learning.py",
    ),
    ("LLM call site", "config/model_routing.yaml", "an entry under call_sites"),
)
IMPLEMENTER_CAP = 40


def _last_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else None


def _bases(signature: str) -> tuple[list[str], bool]:
    """Base names of ``class X(...)`` and whether X is a seam."""
    node = ast.parse(signature + ": ...").body[0]
    names = [n for n in map(_last_name, node.bases) if n]
    meta = any(k.arg == "metaclass" and _last_name(k.value) == "ABCMeta" for k in node.keywords)
    return names, meta or "Protocol" in names or "ABC" in names


class _Sig(NamedTuple):
    """A method's call contract (positional counts include ``self``)."""

    is_async: bool
    required: int
    positional: int
    varargs: bool
    by_name: frozenset[str]  # parameters a caller may pass by keyword
    kw_only: frozenset[str]
    kw_required: frozenset[str]
    varkw: bool


def _shape(signature: str) -> _Sig:
    fn = ast.parse(signature + ": ...").body[0]
    a = fn.args
    pos = len(a.posonlyargs) + len(a.args)
    kw_only = frozenset(k.arg for k in a.kwonlyargs)
    kw_required = frozenset(
        k.arg for k, d in zip(a.kwonlyargs, a.kw_defaults, strict=True) if d is None
    )
    by_name = frozenset(x.arg for x in a.args) | kw_only
    is_async = isinstance(fn, ast.AsyncFunctionDef)
    return _Sig(
        is_async,
        pos - len(a.defaults),
        pos,
        a.vararg is not None,
        by_name,
        kw_only,
        kw_required,
        a.kwarg is not None,
    )


def _fits(seam: _Sig, impl: _Sig) -> bool:
    """Whether the implementer passes the call checks the module docstring lists."""
    return (
        impl.is_async == seam.is_async
        and impl.required <= seam.required
        and (impl.positional >= seam.positional or impl.varargs)
        and (impl.varkw or seam.kw_only <= impl.by_name)
        and impl.kw_required <= seam.kw_required
    )


def _attribute_like(seam: _Sig) -> bool:
    """A sync, self-only member (often a property) a class attribute could supply."""
    return not seam.is_async and seam.positional == 1 and not seam.varargs and not seam.kw_only


def find_seams(
    classes: list[dict], methods: list[dict], *, name: str = "", cap: int = IMPLEMENTER_CAP
) -> tuple[list[dict], int]:
    """Each public seam with its methods and implementers, from index rows.

    ``classes``: module_path, name, signature, docstring. ``methods``: public
    method rows with module_path, parent_class, name, signature. ``name``
    selects one seam and lifts the per-seam cap. Returns the seams and the
    number of rows whose stored signature did not parse (skipped, not fatal).
    A class whose signature is still NULL is waiting for a re-index: skipped.
    """
    unparsed = 0
    shapes: dict[tuple[str, str], dict[str, list[tuple]]] = {}
    for m in methods:
        try:
            shape = _shape(m["signature"])
        except SyntaxError:
            unparsed += 1
            continue
        key = (m["module_path"], m["parent_class"])
        shapes.setdefault(key, {}).setdefault(m["name"], []).append(shape)
    info: dict[tuple[str, str], tuple[list[str], bool, dict]] = {}
    for c in classes:
        if c["signature"] is None:
            continue
        try:
            info[(c["module_path"], c["name"])] = (*_bases(c["signature"]), c)
        except SyntaxError:
            unparsed += 1
    defined: dict[str, int] = {}
    seams_named: dict[str, list[tuple[str, str]]] = {}
    for key, (_, is_seam, _) in info.items():
        defined[key[1]] = defined.get(key[1], 0) + 1
        if is_seam:
            seams_named.setdefault(key[1], []).append(key)

    def contract(key: tuple[str, str], seen: set, flags: dict) -> dict[str, _Sig]:
        """The seam's own methods over those it inherits from parent seams.

        Bases are applied last to first, so an earlier base wins as in Python's
        MRO. A parent name defined by more than one seam cannot be resolved, so
        nothing is inherited through it and the contract is flagged.
        """
        seen.add(key)
        members: dict[str, _Sig] = {}
        for base in reversed(info[key][0]):
            parents = seams_named.get(base, [])
            if len(parents) > 1:
                flags["ambiguous"] = True
            elif parents and parents[0] not in seen:
                members.update(contract(parents[0], seen, flags))
        members.update({n: s[0] for n, s in shapes.get(key, {}).items()})
        return members

    order = {"nominal": 0, "extends": 1, "structural": 2}
    out = []
    for key, (_, is_seam, row) in sorted(info.items(), key=lambda kv: (kv[0][1], kv[0][0])):
        if not is_seam or key[1].startswith("_") or (name and key[1] != name):
            continue
        flags: dict = {}
        wanted = contract(key, set(), flags)
        found, possible = [], []
        for other, (bases, other_is_seam, _) in info.items():
            if other == key or other[1].startswith("_"):
                continue
            link = {"name": other[1], "module": other[0]}
            if key[1] in bases:
                sure = defined[key[1]] == 1
                kind = "extends" if other_is_seam else "nominal"
                found.append({**link, "match": kind, "confidence": "high" if sure else "low"})
                continue
            if other_is_seam or not wanted:
                continue
            have = shapes.get(other, {})
            met = {n for n, s in wanted.items() if any(_fits(s, i) for i in have.get(n, ()))}
            missing = sorted(set(wanted) - met)
            if not missing:
                low = len(wanted) == 1
                found.append(
                    {**link, "match": "structural", "confidence": "low" if low else "high"}
                )
            elif all(_attribute_like(wanted[n]) for n in missing) and any(
                not _attribute_like(wanted[n]) for n in met
            ):
                possible.append({**link, "missing": missing})
        found.sort(key=lambda f: (order[f["match"]], f["module"], f["name"]))
        entry = {
            "seam": key[1],
            "module": key[0],
            "methods": sorted(wanted),
            "use_when": (row["docstring"] or "").split("\n")[0][:160],
            "implementers": found if name else found[:cap],
        }
        if not name and len(found) > cap:
            entry["implementers_omitted"] = len(found) - cap
        if flags.get("ambiguous"):
            entry["contract_ambiguous"] = True
        if possible:
            entry["possible"] = sorted(possible, key=lambda p: (p["module"], p["name"]))
        out.append(entry)
    return out, unparsed


async def load_seams(db: aiosqlite.Connection, *, name: str = "") -> dict:
    """The seams view: seams, registries, and whether the index is complete."""
    cursor = await db.execute(
        "SELECT module_path, name, signature, docstring FROM code_symbols "
        "WHERE symbol_type = 'class'"
    )
    classes = [dict(r) for r in await cursor.fetchall()]
    cursor = await db.execute(
        "SELECT module_path, parent_class, name, signature FROM code_symbols "
        "WHERE symbol_type IN ('method', 'async_method') AND is_public = 1"
    )
    methods = [dict(r) for r in await cursor.fetchall()]
    cursor = await db.execute("SELECT COUNT(*) FROM code_modules")
    indexed = (await cursor.fetchone())[0]
    pending = sum(1 for c in classes if c["signature"] is None)
    seams, unparsed = find_seams(classes, methods, name=name)
    # An index that has never run, or rows that were dropped, is not a
    # complete answer: an empty result then is not a "no".
    complete = pending == 0 and indexed > 0 and unparsed == 0
    if name and not seams:
        return {
            "error": f"No seam named {name}",
            "complete": complete,
            "indexed_modules": indexed,
            "pending_reindex": pending,
            "unparsed": unparsed,
        }
    return {
        "level": "seams",
        "complete": complete,
        "indexed_modules": indexed,
        "pending_reindex": pending,
        "unparsed": unparsed,
        "seams": seams,
        "registries": [{"kind": k, "add_here": p, "how": h} for k, p, h in REGISTRIES],
    }
