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

``compose`` validates the fields, derives the basis from the PR body's first pointer
line (the acceptance gate's parser) or, with none, from the one issue GitHub links to
the PR (``--basis``, when given, must agree), refuses a
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
from collections.abc import Callable
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
#: A citation is `path:line` or `path:start-end` in backticks. The path may hold
#: spaces (GitHub filenames can); it runs to the LAST colon before the line number.
_CITE_RE = re.compile(r"`([^`\n]+):[1-9][0-9]*(?:-[1-9][0-9]*)?`")
#: A pointer keyword and an issue number. Read on the visible text only (the shared
#: readable-body view, so fenced examples and HTML comments never count).
_KEYWORD = r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs|part[ \t]+of)[ \t]+#([1-9][0-9]*)(?![0-9A-Za-z_])"
#: A visible line that OPENS with one, followed by more text ("Part of #4677. The diff
#: is ..."), which the pointer parser does not read because the line is not only the
#: pointer. MEASURED over 400 recent PR bodies: 24 name their basis this way.
#: Built on the pointer parser's own line rules (acceptance_declaration.py): indent 0-3
#: (4+ is indented code), at most one `>` or list-marker wrapper, ASCII case folding.
_LEAD_POINTER_RE = re.compile(
    r" {0,3}(?:>[ \t]*|(?:[-*+]|[0-9]{1,9}[.)])[ \t]+)?[*_]{0,2}" + _KEYWORD,
    re.IGNORECASE | re.ASCII,
)
#: The same keyword anywhere in a line. Only mid-line is this ambiguous (9 of the 400:
#: "Depends on #4599; ... Refs #4193."), so the writer refuses rather than guess.
#: The refusal net is WIDER than the opener, since refusing is the safe direction: it
#: also takes a colon, `owner/repo#N`, an issue URL and a keyword after `_` or `*`.
_INLINE_POINTER_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?|refs|part[ \t]+of)[ \t]*:?[ \t]+"
    r"(?:#|[\w.-]+/[\w.-]+#|https?://github\.com/[\w.-]+/[\w.-]+/issues/)[0-9]+",
    re.IGNORECASE | re.ASCII,
)
_DONE_RE = re.compile(r"[ \t]*-[ \t]+DONE:")
#: Anything a reader would take for a DONE line. Each one must be in the exact form
#: above, so a numbered, quoted, bold or lower-case variant cannot skip the check.
_DONE_LOOSE_RE = re.compile(
    r"[ \t>]*(?:[-*+]|[0-9]+[.)])?[ \t]*[*_]*done[*_]*[ \t]*[*_]*:", re.IGNORECASE
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
        if len(m.group(1).splitlines()) > 1:  # any line separator, U+2028 included
            problem, tokens = "the header must be one line", []
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


def basis_from_body(body: str | None, *, linked: Callable[[], list[dict]], repo: str) -> str:
    """The marker's ``basis`` for a PR: the body's first pointer line, read by
    ``parse_acceptance``; with none, the one issue GitHub links from the PR.

    ``linked`` returns GitHub's ``closingIssuesReferences`` for the PR, as
    ``[{"number", "repo"}]`` (``linked_issues``); it is called only when the body has no
    pointer line, so a good ``Closes #N`` never depends on that read. GitHub links a closing
    keyword anywhere in the body ("Fixes #4245." mid-sentence), which the pointer
    parser, reading whole lines, does not; asking GitHub covers those without a second
    hand-written grammar. Required, with no default, so a caller cannot skip it and
    silently derive ``claim``. Several linked issues, or one in another repository,
    refuse: the session must say which one the read was against.
    """
    parsed = acceptance_declaration.parse_acceptance(body or "")
    source = parsed["source"]
    if source is not None:
        return f"{_KIND[source['kind']]}:{source['value']}"
    other = [p for p in parsed["problems"] if p not in (_NO_POINTER, "empty PR body")]
    other = [p for p in other if not p.startswith(("no ## Acceptance", "## Acceptance has"))]
    if other:
        raise Refused(f"the PR body's source pointer cannot be read: {'; '.join(other)}")
    linked = linked()
    if not linked:
        # GitHub links closing keywords only, and only on some PRs. A body that names
        # an issue in a form it did not link would otherwise be judged as its own claim
        # (owner ruling, 2026-11-15): a line that opens with the pointer names the basis;
        # a mention only mid-line refuses.
        readable = acceptance_declaration._readable_body()
        if readable is None:
            raise Refused("cannot load readable_body from check_cc_pin_receipts.py")
        visible = readable(body or "", keep_blank=True)
        for line in visible.split("\n"):
            if m := _LEAD_POINTER_RE.match(line):
                return f"issue:{int(m.group(1))}"
        if m := _INLINE_POINTER_RE.search(visible):
            raise Refused(
                f"the PR body says '{m.group(0)}' inside a sentence and GitHub links no issue:"
                " put that pointer on a line of its own, then compose again"
            )
        return "claim"
    if len(linked) > 1:
        names = ", ".join(f"{i['repo']}#{i['number']}" for i in linked)
        raise Refused(
            f"GitHub links several issues to this PR ({names}): put the one the read is"
            " against on a line of its own ('Closes #N'), then compose again"
        )
    [issue] = linked
    if issue["repo"].lower() != repo.lower():
        raise Refused(
            f"the PR's linked issue is {issue['repo']}#{issue['number']}, in another"
            " repository: a marker names an issue in this one, so put that issue's"
            " pointer ('Closes #N') on a line of its own, or record the PR's own claim"
        )
    return f"issue:{issue['number']}"


def linked_issues(repo: str, pr: int) -> list[dict]:
    """GitHub's ``closingIssuesReferences`` for the PR, as ``[{"number", "repo"}]``."""
    raw = _gh(
        [
            "pr",
            "view",
            str(pr),
            "--repo",
            repo,
            "--json",
            "closingIssuesReferences",
            "--jq",
            '.closingIssuesReferences[] | {number, repo: (.repository.owner.login + "/"'
            " + .repository.name)} | @json",
        ]
    )
    rows = _jsonl(raw)  # MEASURED: gh prints each @json result as one plain object line
    for row in rows:
        num = row.get("number") if isinstance(row, dict) else None
        if (
            not isinstance(num, int)
            or isinstance(num, bool)
            or num < 1
            or not _REPO_RE.fullmatch(str(row.get("repo")))
        ):
            raise Refused(f"unreadable linked-issue row from gh: {_safe(row)[:80]}")
    return rows


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


def _basis(repo: str, pr: int, meta: dict) -> str:
    return basis_from_body(meta.get("body"), linked=lambda: linked_issues(repo, pr), repo=repo)


def compose(a: argparse.Namespace) -> int:
    repo = _repo(a.repo)
    owner = repo.split("/")[0].lower()
    # Readers trust only the owner's marker, so anyone else's would be ignored: a
    # non-owner session reports its read and posts nothing (spec 4.3).
    me = _gh(["api", "user", "--jq", ".login"]).strip().lower()
    if me != owner:
        # On an org-owned repo no login equals the org's name, and GitHub gives an org
        # member's comments MEMBER or COLLABORATOR, never OWNER (measured on two public
        # org repos), so readers would ignore any marker posted there.
        if _gh(["api", f"repos/{repo}", "--jq", ".owner.type"]).strip() == "Organization":
            raise Refused(
                f"{repo} is owned by an organization: readers trust only the owner's login or"
                " the OWNER association, which GitHub gives no org member, so a marker here"
                " would be ignored; report the read instead of posting"
            )
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
    basis = _basis(repo, a.pr, meta)
    if a.basis and a.basis != basis:
        raise Refused(
            f"--basis {a.basis} disagrees with the PR's basis ({basis}: its pointer line, or the issue GitHub links)"
        )
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
    again = _pr(repo, a.pr)
    if again["head"] != head:
        raise Refused("the PR's head moved while composing: read the change since")
    if _basis(repo, a.pr, again) != basis:
        raise Refused(
            "the PR's basis changed while composing (its body or links were edited): read it again"
        )
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
    meta = _pr(repo, a.pr)
    head = meta["head"]
    owner = repo.split("/")[0].lower()
    try:
        current, why = _basis(repo, a.pr, meta), ""
    except Refused as exc:
        current, why = None, str(exc)
    print(f"PR #{a.pr} head {head[:12]} basis {current or 'unreadable: ' + _safe(why)}")
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
                    if current is None:
                        at += ", the PR's basis cannot be derived now: re-read"
                    elif f["basis"] != current:
                        at += f", but the PR's basis is now {current}: re-read"
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
    c.add_argument("--basis", help="assert the basis; it must match the one derived from the PR")
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
