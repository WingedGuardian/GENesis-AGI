#!/usr/bin/env python3
"""Write and read the delivery-check marker on a PR.

A delivery read answers one question about a PR: does it deliver what its basis (the
issue, ledger row, follow-up, spec or plan it points at, or its own claim) asks for?
The session doing the read records the verdict as ONE owner-authored PR comment,
edited in place when the read is redone. The comment opens with a closed header:

    <!-- genesis-delivery-check: head=<40 hex> basis=<B> verdict=<V> scope=<S>
         [intended=<I>] [change=<C>] [coderabbit=<R>] [checker=<K>] [outcome=<O>] -->

(on one line). Fixed keys, each value matched whole; an unknown key or value, a
repeated key, or a missing required one makes the block unusable, and the problem
quotes the field. The prose under the header is never parsed by a reader.

Usage:
    delivery_marker.py compose --pr N --head SHA --verdict V --scope S --checker K
                               --body-file PROSE [--intended I] [--change C]
                               [--coderabbit R] [--outcome O] [--basis B]
                               [--repo OWNER/REPO] [--out FILE]
    delivery_marker.py read --pr N [--repo OWNER/REPO]

``compose`` validates the fields, derives the basis from the PR body with the same
pointer parser the acceptance gate uses (``--basis``, when given, must agree), refuses a
DELIVERS whose DONE lines cite no ``path:line`` or cite a file the PR does not change,
writes the comment to a file, and PRINTS the one ``gh`` command that posts it: a PATCH
of the owner's existing marker comment, or a new comment. It never posts.

``read`` lists every marker block on the PR's comments and reviews with how a reader
treats it. ``parse(text)`` is the one parser; the gate row imports it.

Stdlib only. Exit 0 on success, 2 on a refusal (the reason on stderr).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))  # so an importer in scripts/hooks finds the sibling

import acceptance_declaration  # noqa: E402

#: A marker is the header that OPENS a comment body. A header quoted further down (a
#: merge question citing the format, a fenced template) is not a marker: reading those
#: would let any comment that mentions the format make a PR read as malformed, and let
#: compose pick that comment as the one to overwrite. The match is case-insensitive and
#: spacing-tolerant so a near-miss header is reported, not silently invisible.
LEAD_RE = re.compile(r"\A\s*<!--\s*genesis-delivery-check\s*:(.*?)-->", re.DOTALL | re.IGNORECASE)
_EXACT_RE = re.compile(r"\A\s*<!--\s*genesis-delivery-check:")

_NAME = r"[A-Za-z0-9._-]+"
VALUES: dict[str, re.Pattern[str]] = {
    "head": re.compile(r"[0-9a-f]{40}"),
    "basis": re.compile(
        rf"issue:[1-9][0-9]*|ledger:[0-9a-f]{{32}}|follow-up:[0-9a-f]{{32}}"
        rf"|spec:{_NAME}|plan:{_NAME}|claim"
    ),
    "verdict": re.compile(r"DELIVERS|PARTIAL|DOES-NOT|UNCLEAR"),
    "scope": re.compile(r"CLEAN|NEEDED|DRIFT"),
    "intended": re.compile(r"read|not-stated|unsupported|n/a"),
    "change": re.compile(r"stated|absent|n/a"),
    "coderabbit": re.compile(r"passed|warning|inconclusive|absent"),
    "checker": re.compile(r"other|author-review"),
    "outcome": re.compile(r"converged|converged-with-carried|open|n/a"),
}
REQUIRED = ("head", "basis", "verdict", "scope")
#: The verdict each spec-converge Outcome stands for (an author-review marker only).
_OUTCOME_VERDICT = {
    "converged": "DELIVERS",
    "converged-with-carried": "DELIVERS",
    "open": "PARTIAL",
}

#: GitHub's REST list of a PR's files stops at 3,000 entries.
FILE_CAP = 3000
_CITE_RE = re.compile(r"`([^`\s]+):[1-9][0-9]*(?:-[1-9][0-9]*)?`")
_DONE_RE = re.compile(r"[ \t]*-[ \t]+DONE:")
#: Anything a reader would take for a DONE line. Each one must be in the exact form
#: above, so a numbered, quoted, bold or lower-case variant cannot skip the check.
_DONE_LOOSE_RE = re.compile(
    r"[ \t>]*(?:[-*+]|[0-9]+[.)])?[ \t]*[*_]*done[*_]*[ \t]*[*_]*:", re.IGNORECASE
)
#: A closing keyword inside a sentence, which the pointer parser does not read.
_INLINE_CLOSE_RE = re.compile(
    r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)[ \t]+#[0-9]+", re.IGNORECASE
)
#: The comment is public and no hook scans a comment body, so the writer refuses the
#: plainest private shapes: private and tailnet IPv4 addresses, home-directory paths
#: (they carry a username) and the secrets file's name. A floor, not a secret scanner.
_PRIVATE_RE = re.compile(
    r"\b(?:10|127)\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"
    r"|\b192\.168\.\d{1,3}\.\d{1,3}\b"
    r"|\b172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}\b"
    r"|\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"
    r"|(?:/home/|/Users/)[^/\s`]+|\bsecrets\.env\b"
)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def _safe(value: object) -> str:
    """``value`` as text with control characters escaped, for printing GitHub data."""
    return _CONTROL_RE.sub(lambda m: f"\\x{ord(m.group(0)):02x}", str(value))


_KIND = {
    "issue": "issue",
    "ledger": "ledger",
    "follow_up": "follow-up",
    "spec": "spec",
    "plan": "plan",
}
#: What parse_acceptance says when the body simply has no pointer: the basis is the claim.
_NO_POINTER = acceptance_declaration._NO_POINTER


class Refused(Exception):
    """A reason compose or read stops, printed to stderr."""


def _check(fields: dict[str, str]) -> str | None:
    """Fill the stated defaults into ``fields`` and return the first problem, or None."""
    for key in REQUIRED:
        if key not in fields:
            return f"missing required field '{key}'"
    on_issue = fields["basis"].startswith("issue:")
    if "intended" not in fields:
        if on_issue:
            return "missing 'intended' on an issue basis"
        fields["intended"] = "n/a"
    elif not on_issue and fields["intended"] != "n/a":
        return f"intended={fields['intended']} needs an issue basis (use n/a)"
    fields.setdefault("checker", "author-review")  # absent is never read as `other`
    outcome = fields.get("outcome")
    if outcome and outcome != "n/a":
        if fields["checker"] != "author-review":
            return f"outcome={outcome} belongs to an author-review read only"
        if _OUTCOME_VERDICT[outcome] != fields["verdict"]:
            return f"outcome={outcome} means verdict={_OUTCOME_VERDICT[outcome]}, not {fields['verdict']}"
    return None


def parse(text: str) -> list[dict]:
    """The marker block that opens ``text``, as ``[{"fields", "problem", "raw"}]``, or
    ``[]`` when the text does not open with one.

    ``fields`` is the validated dict (stated defaults filled in) and ``problem`` None, or
    ``fields`` is None and ``problem`` says why the block is unusable. Never raises.
    """
    out = []
    m = LEAD_RE.match(text) if isinstance(text, str) else None
    if m:
        raw = m.group(1).strip()
        fields: dict[str, str] = {}
        problem = None if raw else "the block names no fields"
        tokens = raw.split()
        if not _EXACT_RE.match(text):
            problem = "the header must open with '<!-- genesis-delivery-check:' exactly"
            raw, tokens = m.group(0).strip(), []
        for token in tokens:
            key, sep, value = token.partition("=")
            if not sep:
                problem = f"'{token}' is not key=value"
            elif key not in VALUES:
                problem = f"unknown key '{key}' in '{token}'"
            elif key in fields:
                problem = f"key '{key}' given twice"
            elif not VALUES[key].fullmatch(value):
                problem = f"'{token}' is not a valid {key}"
            else:
                fields[key] = value
                continue
            break
        problem = problem or _check(fields)
        out.append({"fields": None if problem else fields, "problem": problem, "raw": raw})
    return out


def basis_from_body(body: str | None) -> str:
    """The marker's ``basis`` value for a PR body, read by ``parse_acceptance``."""
    parsed = acceptance_declaration.parse_acceptance(body or "")
    source = parsed["source"]
    if source is None:
        other = [p for p in parsed["problems"] if p not in (_NO_POINTER, "empty PR body")]
        other = [p for p in other if not p.startswith(("no ## Acceptance", "## Acceptance has"))]
        if other:
            raise Refused(f"the PR body's source pointer cannot be read: {'; '.join(other)}")
        # The pointer parser reads a pointer only as a line of its own. A body that
        # names an issue mid-sentence would otherwise be judged as its own claim, which
        # skips the issue's acceptance and the proposal rule. The gate reads the same
        # parser, so the fix is the body, not a smarter writer.
        if m := _INLINE_CLOSE_RE.search(body or ""):
            raise Refused(
                f"the PR body says '{m.group(0)}' inside a sentence, which the pointer parser"
                " does not read: put it on a line of its own, then compose again"
            )
        return "claim"
    return f"{_KIND[source['kind']]}:{source['value']}"


def evidence_problems(verdict: str, prose: str, changed: set[str]) -> list[str]:
    """Why a DELIVERS's prose does not carry its evidence; [] when it does or the
    verdict is not DELIVERS. Only DONE lines are read; each must cite a changed file."""
    if verdict != "DELIVERS":
        return []
    done = [ln for ln in prose.splitlines() if _DONE_LOOSE_RE.match(ln)]
    if not done:
        return ["a DELIVERS needs at least one '- DONE: <point> (`path:line`)' line"]
    problems = [
        f"write DONE lines as '- DONE: ...', not: {ln.strip()}"
        for ln in done
        if not _DONE_RE.match(ln)
    ]
    for line in done:
        cites = _CITE_RE.findall(line)
        if not cites:
            problems.append(f"DONE line cites no `path:line`: {line.strip()}")
        for path in cites:
            if path not in changed:
                problems.append(f"DONE line cites {path}, which this PR does not change")
    return problems


def _basis_words(basis: str) -> str:
    kind, _, value = basis.partition(":")
    return {
        "issue": f"issue #{value}",
        "ledger": f"ledger row {value[:12]}",
        "follow-up": f"follow-up {value[:12]}",
        "spec": f"spec `{value}`",
        "plan": f"plan `{value}`",
    }.get(kind, "the PR's own claim")


def render(fields: dict[str, str], prose: str) -> str:
    """The comment body: the header, a generated first line, then the prose."""
    head = " ".join(f"{k}={fields[k]}" for k in VALUES if k in fields)
    return (
        f"<!-- genesis-delivery-check: {head} -->\n"
        f"**Delivery check** at `{fields['head'][:12]}` against {_basis_words(fields['basis'])}\n"
        f"{prose.rstrip()}\n"
    )


# ── GitHub reads (the one seam tests replace) ────────────────────────────────


def _gh(args: list[str]) -> str:
    try:
        r = subprocess.run(["gh", *args], capture_output=True, text=True)
    except OSError as exc:
        raise Refused(f"cannot run gh: {exc}") from exc
    if r.returncode != 0:
        raise Refused(f"gh {' '.join(args[:2])} failed: {_safe(r.stderr.strip()[:300])}")
    return r.stdout


def _jsonl(raw: str) -> list[dict]:
    rows = []
    for line in raw.split("\n"):  # not splitlines: U+2028 may sit raw inside a JSON string
        if line.strip():
            try:
                rows.append(json.loads(line))
            except ValueError as exc:
                raise Refused(f"unreadable gh output: {_safe(line[:80])}") from exc
    return rows


def _repo(repo: str | None) -> str:
    repo = repo or _gh(["repo", "view", "--json", "nameWithOwner", "-q", ".nameWithOwner"]).strip()
    if not _REPO_RE.fullmatch(repo) or ".." in repo:
        raise Refused(f"not an OWNER/REPO: {_safe(repo)}")
    return repo


def _pr(repo: str, pr: int) -> dict:
    try:
        meta = json.loads(
            _gh(["api", f"repos/{repo}/pulls/{pr}", "--jq", "{head: .head.sha, body: .body}"])
        )
    except ValueError as exc:
        raise Refused("unreadable PR metadata from gh") from exc
    if not isinstance(meta, dict) or not VALUES["head"].fullmatch(str(meta.get("head"))):
        raise Refused("unreadable PR metadata from gh")
    return meta


_ROW = "{id, login: .user.login, author_association, body, stamp: %s}"


def _comments(repo: str, pr: int, *, reviews: bool = False) -> list[dict]:
    if reviews:
        path, stamp = f"repos/{repo}/pulls/{pr}/reviews", ".submitted_at"
    else:
        path, stamp = f"repos/{repo}/issues/{pr}/comments", ".updated_at"
    rows = _jsonl(_gh(["api", path, "--paginate", "--jq", ".[] | " + _ROW % stamp]))
    for row in rows:
        # The id goes into a printed API path: only GitHub's integer ids are accepted.
        cid = row.get("id") if isinstance(row, dict) else None
        if not isinstance(cid, int) or isinstance(cid, bool):
            raise Refused(f"unreadable comment row from gh: {_safe(row)[:80]}")
    return rows


def _is_owner(row: dict, owner: str) -> bool:
    return (row.get("login") or "").lower() == owner or (
        row.get("author_association") or ""
    ).upper() == "OWNER"


# ── commands ─────────────────────────────────────────────────────────────────


def compose(a: argparse.Namespace) -> int:
    repo = _repo(a.repo)
    owner = repo.split("/")[0].lower()
    # Readers trust only the owner's marker, so anyone else's would be ignored: a
    # non-owner session reports its read and posts nothing (spec 4.3).
    me = _gh(["api", "user", "--jq", ".login"]).strip().lower()
    if me != owner:
        raise Refused(
            f"gh is authenticated as '{_safe(me)}', not the repo owner '{owner}': a marker from"
            " this account is ignored by every reader, so report the read instead of posting"
        )
    meta = _pr(repo, a.pr)
    head = meta["head"]
    read_head = a.head.lower()
    if not (re.fullmatch(r"[0-9a-f]{7,40}", read_head) and head.startswith(read_head)):
        raise Refused(
            f"the PR's head is now {head[:12]}, not the {a.head} you read: read the change since"
        )
    basis = basis_from_body(meta.get("body"))
    if a.basis and a.basis != basis:
        raise Refused(f"--basis {a.basis} disagrees with the PR body's pointer ({basis})")
    fields = {
        "head": head,
        "basis": basis,
        "verdict": a.verdict,
        "scope": a.scope,
        "checker": a.checker,
    }
    for key in ("intended", "change", "coderabbit", "outcome"):
        if getattr(a, key):
            fields[key] = getattr(a, key)
    [block] = parse(render(fields, ""))
    if block["problem"]:
        raise Refused(block["problem"])
    try:
        prose = Path(a.body_file).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise Refused(f"cannot read the prose file: {exc}") from exc
    if not prose.strip():
        raise Refused("the prose file is empty: write the read's DONE/PARTIAL lines first")
    if m := _PRIVATE_RE.search(prose):
        raise Refused(f"the prose names '{m.group(0)}', which is private: the comment is public")
    if "<!--" in prose:
        raise Refused("the prose may not contain an HTML comment ('<!--'): only the header is one")
    # One JSON string per line, so a filename holding a newline cannot forge an entry.
    raw = _gh(
        ["api", f"repos/{repo}/pulls/{a.pr}/files", "--paginate", "--jq", ".[].filename | @json"]
    )
    files = _jsonl(raw)
    if not all(isinstance(f, str) for f in files):
        raise Refused("unreadable file list from gh")
    if len(files) >= FILE_CAP:
        raise Refused(
            f"the PR lists {len(files)} files, GitHub's cap of {FILE_CAP}: its changed files cannot be confirmed"
        )
    # The file list is read after the head check, so a push in between would check the
    # citations against a tree nobody read. Re-read the head and refuse if it moved.
    if _pr(repo, a.pr)["head"] != head:
        raise Refused("the PR's head moved while composing: read the change since")
    problems = evidence_problems(a.verdict, prose, set(files))
    if problems:
        raise Refused("; ".join(problems))
    mine = [
        c
        for c in _comments(repo, a.pr)
        if _is_owner(c, owner) and LEAD_RE.match(c.get("body") or "")
    ]
    home = Path(os.environ.get("GENESIS_HOME") or Path.home() / ".genesis")
    out = (Path(a.out) if a.out else home / "delivery" / f"pr-{a.pr}-{head[:12]}.md").resolve()
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(render(fields, prose), encoding="utf-8")
    except OSError as exc:
        raise Refused(f"cannot write {out}: {exc}") from exc
    if mine:
        latest = max(mine, key=lambda c: c.get("stamp") or "")
        cmd = [
            "gh",
            "api",
            "--method",
            "PATCH",
            f"repos/{repo}/issues/comments/{latest['id']}",
            "-F",
            f"body=@{out}",
        ]
        print(f"Wrote {out}. It replaces your marker comment {latest['id']}; post it with:")
    else:
        cmd = ["gh", "pr", "comment", str(a.pr), "--repo", repo, "--body-file", str(out)]
        print(f"Wrote {out}. Post it with:")
    print(shlex.join(cmd))
    return 0


def read(a: argparse.Namespace) -> int:
    repo = _repo(a.repo)
    head = _pr(repo, a.pr)["head"]
    owner = repo.split("/")[0].lower()
    print(f"PR #{a.pr} head {head[:12]}")
    seen = 0
    for where, rows in (
        ("comment", _comments(repo, a.pr)),
        ("review", _comments(repo, a.pr, reviews=True)),
    ):
        for row in rows:
            for block in parse(row.get("body") or ""):
                seen += 1
                label = _safe(f"{where} {row.get('id')} by {row.get('login')} ({row.get('stamp')})")
                if not _is_owner(row, owner):
                    print(f"- {label}: ignored, not the repo owner")
                elif where == "review":
                    print(
                        f"- {label}: ignored, a review body cannot be edited in place; use compose"
                    )
                elif block["problem"]:
                    print(f"- {label}: unusable, {_safe(block['problem'])}")
                else:
                    f = block["fields"]
                    at = (
                        "at the current head"
                        if f["head"] == head
                        else f"at {f['head'][:12]}, not the current head"
                    )
                    print(
                        f"- {label}: {at}: "
                        + " ".join(f"{k}={v}" for k, v in f.items() if k != "head")
                    )
    if not seen:
        print("no delivery check recorded")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compose", help="write the marker comment and print the gh command")
    c.add_argument("--pr", type=int, required=True)
    c.add_argument("--head", required=True, help="the head you read (7 to 40 hex)")
    for key in ("verdict", "scope", "checker"):
        c.add_argument(f"--{key}", required=True, choices=VALUES[key].pattern.split("|"))
    for key in ("intended", "change", "coderabbit", "outcome"):
        c.add_argument(f"--{key}", choices=VALUES[key].pattern.split("|"))
    c.add_argument("--basis", help="assert the basis; it must match the PR body's pointer")
    c.add_argument(
        "--body-file", required=True, help="the read's prose (DONE/PARTIAL lines and the rest)"
    )
    c.add_argument("--out", help="where to write the comment (default $GENESIS_HOME/delivery/)")
    c.add_argument("--repo")
    r = sub.add_parser("read", help="list the PR's marker blocks as a reader treats them")
    r.add_argument("--pr", type=int, required=True)
    r.add_argument("--repo")
    a = p.parse_args(argv)
    try:
        return compose(a) if a.cmd == "compose" else read(a)
    except Refused as exc:
        print(f"delivery_marker: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
