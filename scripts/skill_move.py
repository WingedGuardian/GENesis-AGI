#!/usr/bin/env python3
"""Generate skill files from a row file, and check the move (skill-split T-2).

A row file maps ranges of source files, read at a git ref, to destination
files. Each destination is the concatenation of its pieces in row-file order:
a source range (``source``/``from``/``until``) or an authored file (``file``).
A piece with ``dest: null`` deletes its range and must give a ``reason``.

Sources come from ``--source-ref`` (default ``origin/main``), never the working
tree, because a destination can be its own source: the generated core
overwrites the file it was cut from. To regenerate after main moves (a
conflict in a generated file), take either side, rerun with
``--source-ref origin/main``, ``git add`` the outputs and continue.

Anchors match a line by PREFIX and must match exactly one line of their
source; ``@start``/``@end`` are line 1 and end of file. ``until`` is the first
line NOT in the piece. Rewrites are declared per piece, each ``old`` matched
exactly once, by kind: ``path`` (one path token changes, and the new path
resolves), ``pointer`` (a short phrase containing "above" or "below" made
explicit) or ``edit`` (a declared non-move edit). The report prints every
rewrite in full. Pieces are joined with exactly one blank line; each join that
changed the blank lines is reported.

Every run checks, before writing anything:
  1. every line of every source belongs to exactly one piece;
  2. anchors, rewrites and paths in the row file are valid (repo-relative,
     outside .git, one path per destination);
  3. (``--check``) each output on disk equals its assembly, byte for byte;
  4. paths in outputs resolve: backticked ``references/...md`` from the skill
     root, ``.claude/...`` from the repo root, ``../...`` and markdown link
     targets from the output's directory (a path already unresolvable in its
     source at the ref is reported, not failed);
  5. no output replaces a file that exists at the ref but is no source, and no
     file added since the ref sits under a destination's directory unless it
     is an output (an orphan of a renamed destination);
  6. each output ``SKILL.md`` has frontmatter with ``name`` = its directory,
     ``description`` and ``keywords``; a piece's ``must_end_before`` holds.

Run it in a worktree: sessions in the primary checkout load what it writes.
Exit 0 ok, 1 a check failed, 2 the row file, git or the file system is unusable.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import subprocess
import sys
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import yaml

_PIECE_KEYS = {
    "row",
    "dest",
    "source",
    "from",
    "until",
    "file",
    "rewrites",
    "reason",
    "must_end_before",
}
_KINDS = {"path", "pointer", "edit"}
_POINTER_MAX = 80
_PATH_TOKEN = re.compile(r"(?<![\w./-])[\w.-]*(?:/[\w.-]+)+\.md")
_POINTER = re.compile(r"\b(above|below)\b", re.IGNORECASE)
_BACKTICKED = re.compile(r"`((?:references/|\.claude/|\.\./)[\w./-]+)`")
_MD_LINK = re.compile(r"\[[^\[\]]*\]\(([^)\s]+)\)")


class RowError(Exception):
    """The row file, git or the file system cannot be used: exit 2."""


@dataclass
class Piece:
    index: int
    row: str
    dest: str | None
    source: str | None = None
    frm: str = ""
    until: str = ""
    file: str | None = None
    rewrites: list[dict] = field(default_factory=list)
    must_end_before: int | None = None
    span: tuple[int, int] = (0, 0)
    original: str = ""
    text: str = ""


class _StrictLoader(yaml.SafeLoader):
    """SafeLoader that refuses a mapping with a duplicate key."""


def _no_duplicates(loader, node, deep=False):
    keys = [loader.construct_object(k, deep=deep) for k, _ in node.value]
    dupes = sorted(str(k) for k, n in Counter(keys).items() if n > 1)
    if dupes:
        raise RowError(f"duplicate key(s) {dupes} at line {node.start_mark.line + 1}")
    return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)


_StrictLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _no_duplicates)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def _at_ref(root: Path, ref: str, path: str) -> str | None:
    if _git(root, "cat-file", "-e", f"{ref}:{path}").returncode != 0:
        return None
    shown = _git(root, "show", f"{ref}:{path}")
    if shown.returncode != 0:
        raise RowError(f"git show {ref}:{path}: {shown.stderr.strip()}")
    return shown.stdout


def _text(value, what: str, *, empty_ok: bool = False) -> str:
    # An unquoted `### x` in YAML loads as None; never let that read as @start.
    if not isinstance(value, str) or (not value and not empty_ok):
        raise RowError(f"{what} must be a non-empty quoted string, got {value!r}")
    return value


def _repo_path(value, what: str) -> str:
    rel = _text(value, what)
    pp = PurePosixPath(rel)
    if (
        not pp.parts
        or pp.is_absolute()
        or ".." in pp.parts
        or ".git" in pp.parts
        or pp.as_posix() != rel
    ):
        raise RowError(f"{what}: {rel!r} must be a normalized repo-relative path outside .git")
    return rel


def _confined(root: Path, rel: str, what: str) -> Path:
    """The working-tree path for ``rel``, refusing any symlink on the way.

    ``_repo_path`` already rejected absolute paths, ``..`` and ``.git``; with no
    symlinked component the path cannot leave the repository either.
    """
    probe = root
    for part in PurePosixPath(rel).parts:
        probe = probe / part
        if probe.is_symlink():
            raise RowError(
                f"{what}: {rel} passes through a symlink ({probe.relative_to(root)}); refusing"
            )
    return probe


def _resolve_ref(root: Path, ref: str) -> str:
    """The commit ``ref`` names. A ref is never handed to git as anything else."""
    if not ref:
        raise RowError(f"--source-ref {ref!r} is not a ref")
    out = _git(root, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{ref}^{{commit}}")
    sha = out.stdout.strip()
    if out.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", sha):
        raise RowError(f"--source-ref {ref!r} does not name a commit")
    return sha


def load_rows(path: Path) -> tuple[dict[str, str], list[Piece]]:
    try:
        data = yaml.load(path.read_text(encoding="utf-8"), Loader=_StrictLoader)  # noqa: S506 - SafeLoader subclass
    except (OSError, yaml.YAMLError) as exc:
        raise RowError(f"{path}: {exc}") from exc
    if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
        raise RowError(f"{path}: expected a mapping with version: 1")
    dests = data.get("destinations")
    if not isinstance(dests, dict) or not dests:
        raise RowError("destinations must be a non-empty mapping of name -> path")
    seen: dict[str, str] = {}
    for name, dpath in dests.items():
        dpath = _repo_path(dpath, f"destination {name}")
        if dpath in seen:
            raise RowError(f"destinations {seen[dpath]} and {name} share the path {dpath}")
        seen[dpath] = name
    pieces = []
    raw_pieces = data.get("pieces") or []
    if not isinstance(raw_pieces, list):
        raise RowError("pieces must be a list")
    for i, raw in enumerate(raw_pieces, 1):
        if not isinstance(raw, dict) or set(raw) - _PIECE_KEYS or "dest" not in raw:
            bad = sorted(set(raw) - _PIECE_KEYS) if isinstance(raw, dict) else raw
            raise RowError(f"piece {i}: needs a dest key (null for DELETE); unknown keys {bad}")
        p = Piece(index=i, row=str(raw.get("row", i)), dest=raw["dest"])
        where = f"piece {i} (row {p.row})"
        if p.dest is not None and (not isinstance(p.dest, str) or p.dest not in dests):
            raise RowError(f"{where}: unknown destination {p.dest!r}")
        if (p.dest is None) != ("reason" in raw):
            raise RowError(f"{where}: a reason is required on a DELETE piece and only there")
        if "file" in raw:
            if "source" in raw or p.dest is None or raw.get("rewrites"):
                raise RowError(f"{where}: an authored piece has a file and a destination only")
            p.file = _repo_path(raw["file"], f"{where} file")
        else:
            p.source = _repo_path(raw.get("source"), f"{where} source")
            p.frm = _text(raw.get("from"), f"{where} from")
            p.until = _text(raw.get("until"), f"{where} until")
            if p.dest is None:
                _text(raw["reason"], f"{where} DELETE reason")
        if not isinstance(raw.get("rewrites") or [], list):
            raise RowError(f"{where}: rewrites must be a list")
        for j, rw in enumerate(raw.get("rewrites") or [], 1):
            what = f"{where} rewrite {j}"
            if (
                not isinstance(rw, dict)
                or rw.get("kind") not in _KINDS
                or set(rw) != {"kind", "old", "new"}
            ):
                raise RowError(f"{what}: needs exactly kind (one of {sorted(_KINDS)}), old and new")
            p.rewrites.append(
                {
                    "kind": rw["kind"],
                    "old": _text(rw["old"], f"{what} old"),
                    "new": _text(rw["new"], f"{what} new", empty_ok=rw["kind"] == "edit"),
                }
            )
        if "must_end_before" in raw:
            meb = raw["must_end_before"]
            if type(meb) is not int or meb <= 0:
                raise RowError(f"{where}: must_end_before must be a positive integer")
            p.must_end_before = meb
        pieces.append(p)
    if not pieces:
        raise RowError("no pieces")
    return dests, pieces


def _find(lines: list[str], anchor: str, where: str) -> int:
    if anchor == "@start":
        return 0
    if anchor == "@end":
        return len(lines)
    hits = [n for n, line in enumerate(lines) if line.rstrip().startswith(anchor.rstrip())]
    if len(hits) != 1:
        shown = "; ".join(f"{n + 1}: {lines[n][:60]}" for n in hits[:5]) or "none"
        raise RowError(f"{where}: anchor {anchor!r} matched {len(hits)} lines ({shown})")
    return hits[0]


def _validate_rewrite(rw: dict, where: str) -> None:
    old, new = rw["old"], rw["new"]
    if rw["kind"] == "path":
        po, pn = _PATH_TOKEN.findall(old), _PATH_TOKEN.findall(new)
        if len(po) != 1 or len(pn) != 1 or old.replace(po[0], pn[0]) != new:
            raise RowError(f"{where}: a path rewrite may change one path token and nothing else")
    elif rw["kind"] == "pointer" and (not _POINTER.search(old) or len(old) > _POINTER_MAX):
        raise RowError(
            f"{where}: a pointer rewrite replaces a phrase of at most {_POINTER_MAX} characters "
            "containing 'above' or 'below'; use kind edit for anything larger"
        )


def _lines_trimmed(text: str) -> tuple[str, int, int]:
    """Return (text without leading/trailing blank lines, leading count, trailing count)."""
    lines = text.split("\n")
    lead = trail = 0
    while lines and not lines[0].strip():
        lines.pop(0)
        lead += 1
    while lines and not lines[-1].strip():
        lines.pop()
        trail += 1
    return "\n".join(lines), lead, trail


class _Resolver:
    """Resolve path tokens; existence in the tree, among outputs, or at the ref."""

    def __init__(self, root: Path, commit: str, outputs: set[str], *, label: str):
        self.root, self.commit, self.outputs, self.ref = root, commit, outputs, label
        self._at_ref: dict[str, bool] = {}

    def skill_root(self, rel: str, *, at_ref: bool = False) -> str:
        parts = PurePosixPath(rel).parent.parts
        for k in range(len(parts), 0, -1):
            cand = PurePosixPath(*parts[:k], "SKILL.md").as_posix()
            if self.exists(cand, at_ref=at_ref):
                return PurePosixPath(*parts[:k]).as_posix()
        return PurePosixPath(rel).parent.as_posix()

    def exists(self, rel: str, *, at_ref: bool = False) -> bool:
        if not at_ref:
            return rel in self.outputs or (self.root / rel).exists()
        if rel not in self._at_ref:
            probe = _git(self.root, "cat-file", "-e", f"{self.commit}:{rel.rstrip('/')}")
            self._at_ref[rel] = probe.returncode == 0
        return self._at_ref[rel]

    def target(self, token: str, rel: str, *, at_ref: bool = False) -> str | None:
        token = token.split("#", 1)[0].rstrip("/")
        if not token or re.match(r"[a-z][a-z0-9+.-]*:", token) or token.startswith("/"):
            return None  # an anchor, a URL or an absolute path: not checked
        if token.startswith(".claude/"):
            base = ""
        elif token.startswith("references/"):
            base = self.skill_root(rel, at_ref=at_ref)
        else:
            base = PurePosixPath(rel).parent.as_posix()
        joined = posixpath.normpath(posixpath.join(base, token))
        return joined if not joined.startswith("..") else "<outside the repo>"

    def unresolved(self, text: str, rel: str, *, at_ref: bool = False) -> list[str]:
        tokens = _BACKTICKED.findall(text) + _MD_LINK.findall(text)
        bad = []
        for tok in tokens:
            tgt = self.target(tok, rel, at_ref=at_ref)
            if tgt is not None and (tgt.startswith("<") or not self.exists(tgt, at_ref=at_ref)):
                bad.append(tok)
        return bad


def _frontmatter_problem(rel: str, text: str) -> str | None:
    m = re.match(r"---\n(.*?)\n---\n", text, re.S)
    try:
        meta = yaml.safe_load(m.group(1)) if m else None
    except yaml.YAMLError:
        meta = None
    if not isinstance(meta, dict):
        return "no parseable frontmatter"
    if meta.get("name") != PurePosixPath(rel).parent.name:
        return (
            f"frontmatter name {meta.get('name')!r} != directory {PurePosixPath(rel).parent.name!r}"
        )
    missing = [k for k in ("description", "keywords") if not meta.get(k)]
    return f"frontmatter lacks {', '.join(missing)}" if missing else None


def _cut(
    root: Path,
    commit: str,
    ref: str,
    dests: dict[str, str],
    pieces: list[Piece],
    failures: list[str],
):
    """Read sources at the ref, resolve anchors, apply rewrites, check coverage."""
    sources: dict[str, list[str]] = {}
    for p in pieces:
        if p.source and p.source not in sources:
            text = _at_ref(root, commit, p.source)
            if text is None:
                raise RowError(f"source {p.source} does not exist at {ref}")
            sources[p.source] = text.split("\n")[:-1] if text.endswith("\n") else text.split("\n")
    for name, dpath in dests.items():
        if dpath not in sources and _at_ref(root, commit, dpath) is not None:
            raise RowError(
                f"destination {name} ({dpath}) exists at {ref} but is no piece's source: "
                "name it as a source or choose another path"
            )
    owner: dict[str, list[int | None]] = {s: [None] * len(ls) for s, ls in sources.items()}
    for p in pieces:
        where = f"piece {p.index} (row {p.row})"
        if p.file:
            fp = _confined(root, p.file, f"{where} file")
            if not fp.is_file():
                raise RowError(f"{where}: authored file {p.file} not found")
            p.text = p.original = fp.read_text(encoding="utf-8")
            continue
        lines = sources[p.source]
        start, end = _find(lines, p.frm, where), _find(lines, p.until, where)
        if end <= start:
            raise RowError(f"{where}: until {p.until!r} is not after from {p.frm!r}")
        p.span = (start, end)
        for n in range(start, end):
            if owner[p.source][n] is not None:
                failures.append(
                    f"{p.source}:{n + 1} is in piece {owner[p.source][n]} and piece {p.index}"
                )
            owner[p.source][n] = p.index
        p.text = p.original = "\n".join(lines[start:end])
        for j, rw in enumerate(p.rewrites, 1):
            _validate_rewrite(rw, f"{where} rewrite {j}")
            count = p.text.count(rw["old"])
            if count != 1:
                raise RowError(f"{where} rewrite {j}: old text matched {count} times")
            p.text = p.text.replace(rw["old"], rw["new"])
    for src, marks in owner.items():
        for n, who in enumerate(marks):
            if who is None:
                failures.append(f"{src}:{n + 1} belongs to no piece: {sources[src][n][:70]!r}")
    return sources, owner


def _assemble(name, dpath, mine, res: _Resolver, failures, report):
    """Join one destination's pieces; check links, rewrites and limits; report."""
    text, starts, lines_out = "", [], []
    prev_trail, prev_row = 0, None
    for p in mine:
        body, lead, trail = _lines_trimmed(p.text)
        label = p.file or f"{p.source}:{p.span[0] + 1}-{p.span[1]}"
        if not body:
            lines_out.append(f"    row {p.row}: {label} is empty, skipped")
            continue
        if text:
            if prev_trail + lead != 1:
                lines_out.append(
                    f"    join row {prev_row} | row {p.row}: {prev_trail + lead} blank line(s) -> 1"
                )
            text += "\n\n"
        starts.append((text.count("\n") + 1, p.row))
        text += body
        prev_trail, prev_row = trail, p.row
        first, last = body.split("\n")[0][:50], body.split("\n")[-1][:50]
        lines_out.append(
            f"    row {p.row}: {label} [{first!r} .. {last!r}] ends at char {len(text)}"
        )
        if p.must_end_before is not None and len(text) > p.must_end_before:
            failures.append(
                f"{dpath}: piece row {p.row} ends at char {len(text)}, past {p.must_end_before}"
            )
        for rw in p.rewrites:
            lines_out.append(f"      {rw['kind']}: {rw['old']!r} -> {rw['new']!r}")
            if rw["kind"] == "path":
                tgt = res.target(_PATH_TOKEN.findall(rw["new"])[0], dpath)
                if tgt is not None and (tgt.startswith("<") or not res.exists(tgt)):
                    failures.append(
                        f"{dpath}: row {p.row}: path rewrite target {rw['new']!r} does not resolve"
                    )
        for tok in res.unresolved(body, dpath):
            if p.source and tok in res.unresolved(p.original, p.source, at_ref=True):
                lines_out.append(
                    f"      note: {tok} was already unresolvable in {p.source} at {res.ref}"
                )
            else:
                failures.append(f"{dpath}: row {p.row}: path {tok} does not resolve")
        for m in _POINTER.finditer(body):
            ctx = body[max(0, m.start() - 30) : m.end() + 10].replace("\n", " ")
            lines_out.append(f"      pointer to check: ...{ctx}...")
    if not text:
        failures.append(f"destination {name} has no non-empty pieces")
        return None, starts
    text += "\n"
    if dpath.endswith("SKILL.md") and (problem := _frontmatter_problem(dpath, text)):
        failures.append(f"{dpath}: {problem}")
    report.append(
        f"{name}: {dpath}  {text.count(chr(10))} lines, ~{len(text.encode()) // 4} tokens, {len(mine)} pieces"
    )
    report.extend(lines_out)
    return text, starts


def build(
    root: Path, rows: Path, ref: str, check: bool
) -> tuple[dict[str, str], list[str], list[str]]:
    """Return (outputs path -> text, failures, report lines). Raises RowError."""
    dests, pieces = load_rows(rows)
    commit = _resolve_ref(root, ref)
    for name, dpath in dests.items():
        target = _confined(root, dpath, f"destination {name}")
        if target.exists() and not target.is_file():
            raise RowError(f"destination {name}: {dpath} exists and is not a regular file")
    failures: list[str] = []
    sources, owner = _cut(root, commit, ref, dests, pieces, failures)
    res = _Resolver(root, commit, set(dests.values()), label=ref)
    outputs: dict[str, str] = {}
    starts: dict[str, list[tuple[int, str]]] = {}
    report: list[str] = []
    for name, dpath in dests.items():
        text, starts[dpath] = _assemble(
            name, dpath, [p for p in pieces if p.dest == name], res, failures, report
        )
        if text is not None:
            outputs[dpath] = text
    for src, marks in owner.items():
        deleted = sum(1 for n in marks if n is not None and pieces[n - 1].dest is None)
        covered = sum(1 for n in marks if n is not None) - deleted
        report.append(f"source {src}: {len(marks)} lines, {covered} moved, {deleted} deleted")

    # An orphan: a file ADDED since the ref under a destination's directory that
    # is no output (an old output left behind by a renamed destination). Edits to
    # existing files there are ordinary changes and are not this check's business.
    dirs = sorted({PurePosixPath(d).parent.as_posix() for d in dests.values()})
    added = _git(root, "diff", "--name-only", "--diff-filter=A", commit, "--", *dirs)
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "--", *dirs)
    if added.returncode or untracked.returncode:
        raise RowError(
            f"git diff/ls-files against {ref}: {(added.stderr or untracked.stderr).strip()}"
        )
    for rel in sorted(set(added.stdout.splitlines()) | set(untracked.stdout.splitlines())):
        if rel not in res.outputs:
            failures.append(
                f"{rel}: added since {ref} under a destination directory but is not an output"
            )
    if check:
        for dpath, want in outputs.items():
            on_disk = root / dpath
            have = on_disk.read_bytes() if on_disk.is_file() else None
            if have != want.encode("utf-8"):
                failures.append(_mismatch(dpath, have, want, starts[dpath]))
    return outputs, failures, report


def _mismatch(dpath: str, have: bytes | None, want: str, starts: list[tuple[int, str]]) -> str:
    hint = "(if main moved, regenerate with --source-ref origin/main)"
    if have is None:
        return f"{dpath}: missing; generate it {hint}"
    h, w = have.decode("utf-8", "replace").split("\n"), want.split("\n")
    n = next((k for k in range(min(len(h), len(w))) if h[k] != w[k]), min(len(h), len(w)))
    if n == len(h) == len(w) and h == w:
        return f"{dpath}: differs from its assembly in line endings or encoding {hint}"
    row = next((r for s, r in reversed(starts) if s <= n + 1), "?")
    got = h[n][:60] if n < len(h) else "<end>"
    exp = w[n][:60] if n < len(w) else "<end>"
    return f"{dpath}: differs from its assembly at line {n + 1} (piece row {row}): on disk {got!r}, expected {exp!r} {hint}"


def _write(root: Path, outputs: dict[str, str]) -> None:
    targets = [root / rel for rel in outputs]
    try:
        for target in targets:
            target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RowError(f"cannot create {exc.filename}: {exc.strerror}; nothing written") from exc
    umask = os.umask(0)
    os.umask(umask)
    # Two phases: every temp file is written first, and only when all succeed
    # are they renamed into place, so a failure leaves no output half-updated.
    staged: list[tuple[str, Path]] = []
    try:
        for target, text in zip(targets, outputs.values(), strict=True):
            mode = target.stat().st_mode & 0o777 if target.exists() else 0o666 & ~umask
            fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
            staged.append((tmp, target))
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
                fh.write(text)
            os.chmod(tmp, mode)
    except BaseException as exc:
        for tmp, _ in staged:
            Path(tmp).unlink(missing_ok=True)
        if isinstance(exc, OSError):
            raise RowError(
                f"writing {exc.filename or 'an output'}: {exc.strerror}; no file written"
            ) from exc
        raise
    done = 0
    try:
        for tmp, target in staged:
            os.replace(tmp, target)
            done += 1
    except OSError as exc:
        for tmp, _ in staged[done:]:
            Path(tmp).unlink(missing_ok=True)
        raise RowError(
            f"renaming into {exc.filename}: {exc.strerror}; {done} of {len(staged)} outputs were replaced"
        ) from exc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--rows", type=Path, default=Path(__file__).resolve().parent / "skill_split_rows.yaml"
    )
    ap.add_argument("--source-ref", default="origin/main")
    ap.add_argument("--check", action="store_true", help="check only; write nothing")
    args = ap.parse_args(argv)
    try:
        top = _git(args.rows.resolve().parent, "rev-parse", "--show-toplevel")
        if top.returncode != 0:
            raise RowError(f"{args.rows} is not inside a git repository")
        root = Path(top.stdout.strip())
        outputs, failures, report = build(root, args.rows.resolve(), args.source_ref, args.check)
        print("\n".join(report))
        if failures:
            print(f"\n{len(failures)} check failure(s); nothing written:", file=sys.stderr)
            print("\n".join(f"  {f}" for f in failures), file=sys.stderr)
            return 1
        if not args.check:
            _write(root, outputs)
            print(f"\nwrote {len(outputs)} file(s)")
        else:
            print("\ncheck passed")
    except RowError as exc:
        print(f"skill_move: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - an unexpected error is exit 2, never "check failed"
        print(f"skill_move: unexpected {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
