"""The delivery-check marker writer and parser (scripts/delivery_marker.py).

The grammar is the closed HTML-comment header the delivery read records on a PR:
fixed keys, each value matched whole, an unknown key or value makes the block
unusable and is reported with the field quoted. The prose under it is never parsed,
except that a DELIVERS must cite, on its DONE lines, `path:line` evidence on files
the PR changes. The writer never posts: it prints the one `gh` command that does.
"""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

import pytest

from tests.conftest import private_module

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"

HEAD = "0123456789abcdef0123456789abcdef01234567"
OTHER = "fedcba9876543210fedcba9876543210fedcba98"
HEX32 = "a" * 32


_DM = None


@pytest.fixture(scope="module")
def dm():
    global _DM
    _DM = private_module("delivery_marker", _SCRIPTS / "delivery_marker.py")
    return _DM


def _block(fields: str) -> str:
    return f"<!-- genesis-delivery-check: {fields} -->"


def _good(extra: str = "") -> str:
    return _block(
        f"head={HEAD} basis=issue:4602 verdict=DELIVERS scope=CLEAN intended=read "
        f"checker=other {extra}".strip()
    )


# ── parse: the closed grammar ────────────────────────────────────────────────


def test_parse_reads_a_full_block(dm):
    [b] = dm.parse(_good("change=stated coderabbit=passed outcome=n/a"))
    assert b["problem"] is None
    assert b["fields"] == {
        "head": HEAD,
        "basis": "issue:4602",
        "verdict": "DELIVERS",
        "scope": "CLEAN",
        "intended": "read",
        "checker": "other",
        "change": "stated",
        "coderabbit": "passed",
        "outcome": "n/a",
    }


def test_parse_ignores_the_prose_under_the_block(dm):
    text = _good() + "\n**Delivery check** at x\n- DONE: verdict=BOGUS head=short\n"
    [b] = dm.parse(text)
    assert b["problem"] is None


def test_parse_is_order_tolerant(dm):
    [b] = dm.parse(_block(f"checker=other scope=CLEAN verdict=PARTIAL basis=claim head={HEAD}"))
    assert b["problem"] is None and b["fields"]["verdict"] == "PARTIAL"


@pytest.mark.parametrize(
    "fields, quoted",
    [
        (f"head={HEAD[:12]} basis=claim verdict=DELIVERS scope=CLEAN", f"head={HEAD[:12]}"),
        (f"head={HEAD.upper()} basis=claim verdict=DELIVERS scope=CLEAN", "head="),
        (f"head={HEAD} basis=claim verdict=delivers scope=CLEAN", "verdict=delivers"),
        (f"head={HEAD} basis=claim verdict=DELIVERS scope=CLEAN mood=good", "mood"),
        (f"head={HEAD} basis=claim verdict=DELIVERS scope=CLEANISH", "scope=CLEANISH"),
        (f"head={HEAD} basis=issue:0 verdict=DELIVERS scope=CLEAN intended=read", "basis="),
        (f"head={HEAD} basis=ledger:{HEX32[:31]} verdict=DELIVERS scope=CLEAN", "basis="),
        (f"head={HEAD} basis=spec:a/b verdict=DELIVERS scope=CLEAN", "basis="),
        (f"head={HEAD} basis=claim verdict=DELIVERS scope=CLEAN checker=me", "checker=me"),
        (f"head={HEAD} basis=claim verdict=DELIVERS scope=CLEAN coderabbit=ok", "coderabbit"),
        (f"head={HEAD} basis=claim verdict=DELIVERS", "scope"),
        (f"head={HEAD} head={HEAD} basis=claim verdict=DELIVERS scope=CLEAN", "head"),
        (f"head={HEAD} basis=claim verdict=DELIVERS scope=CLEAN stray", "stray"),
        ("", "no fields"),
    ],
)
def test_parse_reports_unusable_blocks_with_the_field_quoted(dm, fields, quoted):
    [b] = dm.parse(_block(fields))
    assert b["fields"] is None
    assert quoted in b["problem"]


def test_missing_checker_reads_as_author_review(dm):
    [b] = dm.parse(_block(f"head={HEAD} basis=claim verdict=DELIVERS scope=CLEAN"))
    assert b["fields"]["checker"] == "author-review"


def test_missing_intended_is_na_only_off_an_issue_basis(dm):
    [b] = dm.parse(_block(f"head={HEAD} basis=spec:x verdict=DELIVERS scope=CLEAN"))
    assert b["fields"]["intended"] == "n/a"
    [b] = dm.parse(_block(f"head={HEAD} basis=issue:7 verdict=DELIVERS scope=CLEAN"))
    assert b["fields"] is None and "intended" in b["problem"]


def test_intended_other_than_na_needs_an_issue_basis(dm):
    [b] = dm.parse(_block(f"head={HEAD} basis=claim verdict=DELIVERS scope=CLEAN intended=read"))
    assert b["fields"] is None and "intended=read" in b["problem"]


@pytest.mark.parametrize(
    "fields, ok",
    [
        ("verdict=DELIVERS checker=author-review outcome=converged", True),
        ("verdict=DELIVERS checker=author-review outcome=converged-with-carried", True),
        ("verdict=PARTIAL checker=author-review outcome=open", True),
        ("verdict=DELIVERS checker=author-review outcome=open", False),
        ("verdict=PARTIAL checker=author-review outcome=converged", False),
        ("verdict=DELIVERS checker=other outcome=converged", False),
        ("verdict=DELIVERS checker=other outcome=n/a", True),
    ],
)
def test_outcome_agrees_with_verdict_and_checker(dm, fields, ok):
    [b] = dm.parse(_block(f"head={HEAD} basis=claim scope=CLEAN {fields}"))
    assert (b["problem"] is None) is ok


def test_parse_reads_only_the_header_that_opens_the_text(dm):
    assert dm.parse("no marker here <!-- other: x -->") == []
    [b] = dm.parse("\n  " + _good() + "\n" + _block("head=short"))
    assert b["problem"] is None
    # A header quoted further down (a merge question, a fenced template) is not a marker.
    quoted = "Merge question: the format is\n```\n" + _block("head=<40 hex> basis=<B>") + "\n```\n"
    assert dm.parse(quoted) == []
    assert dm.parse("See `" + _good() + "`") == []


@pytest.mark.parametrize(
    "text",
    [
        _good().replace("genesis-delivery-check:", "GENESIS-delivery-check:"),
        _good().replace("genesis-delivery-check:", "genesis-delivery-check :"),
    ],
)
def test_a_near_miss_header_is_reported_not_invisible(dm, text):
    [b] = dm.parse(text)
    assert b["fields"] is None and "exactly" in b["problem"]


def test_parse_of_a_non_string_is_empty(dm):
    assert dm.parse(_good().encode()) == []
    assert dm.parse(None) == []


def test_parse_never_raises(dm):
    for text in [
        "",
        "<!--",
        "<!-- genesis-delivery-check:",
        _block("=" * 5),
        _block("a=b=c"),
        _block("head=" + "\x00" * 40),
        "<!-- genesis-delivery-check: -->" * 3,
    ]:
        assert isinstance(dm.parse(text), list)


# ── basis: derived from the PR body by the shared pointer parser ─────────────


@pytest.mark.parametrize(
    "body, basis",
    [
        ("Closes #4602\n", "issue:4602"),
        ("Part of #12\n", "issue:12"),
        (f"Ledger: {HEX32}\n", f"ledger:{HEX32}"),
        (f"Follow-up: {HEX32}\n", f"follow-up:{HEX32}"),
        ("Spec: delivery-check\n", "spec:delivery-check"),
        ("Plan: crispy-owl\n", "plan:crispy-owl"),
        ("Just a description.\n", "claim"),
    ],
)
def test_basis_from_body(dm, body, basis):
    assert dm.basis_from_body(body, linked=lambda: [], repo="Own/repo") == basis


def test_basis_refuses_a_body_the_pointer_parser_cannot_read(dm):
    with pytest.raises(dm.Refused, match="pointer"):
        dm.basis_from_body("Spec: a/b\n", linked=lambda: [], repo="Own/repo")


# ── evidence: a DELIVERS cites changed lines on its DONE lines ───────────────


def test_delivers_needs_a_done_line_with_a_changed_path(dm):
    files = {"scripts/x.py"}
    assert dm.evidence_problems("DELIVERS", "- DONE: fixes it (`scripts/x.py:12`)\n", files) == []
    assert dm.evidence_problems("DELIVERS", "Looks right.\n", files)
    assert dm.evidence_problems("DELIVERS", "- DONE: fixes it\n", files)
    probs = dm.evidence_problems(
        "DELIVERS", "- DONE: a (`scripts/x.py:1`)\n- DONE: b (`y.py:3`)\n", files
    )
    assert probs and "y.py" in probs[0]


def test_evidence_accepts_ranges_and_ignores_other_lines(dm):
    files = {"a.py"}
    prose = "- DONE: a (`a.py:3-9`)\nWrong fix named by the issue: none named (`b.py:1`)\n"
    assert dm.evidence_problems("DELIVERS", prose, files) == []


def test_other_verdicts_need_no_evidence(dm):
    assert dm.evidence_problems("PARTIAL", "- PARTIAL: x: missing y\n", set()) == []


# ── render: marker line, generated header, then the prose ────────────────────


def test_render_round_trips_through_parse(dm):
    fields = {
        "head": HEAD,
        "basis": "issue:4602",
        "verdict": "DELIVERS",
        "scope": "CLEAN",
        "intended": "read",
        "checker": "other",
    }
    text = dm.render(fields, "- DONE: x (`a.py:1`)\n")
    [b] = dm.parse(text)
    assert b["problem"] is None and b["fields"]["basis"] == "issue:4602"
    lines = text.splitlines()
    assert lines[0].startswith("<!-- genesis-delivery-check: head=")
    assert lines[1] == f"**Delivery check** at `{HEAD[:12]}` against issue #4602"


# ── compose: the CLI, end to end through the gh seam ─────────────────────────


def dm_refused(msg):
    from tests.test_scripts import test_delivery_marker as me  # noqa: PLC0415

    return me._DM.Refused(msg)


class FakeGh:
    def __init__(
        self,
        *,
        head=HEAD,
        body="Closes #4602\n",
        files=("scripts/x.py",),
        comments=(),
        reviews=(),
        me="own",
        heads=(),
        linked=(),
        bodies=(),
        owner_type="User",
        role=None,
    ):
        self.head, self.body, self.files, self.comments = head, body, list(files), list(comments)
        self.reviews = list(reviews)
        self.me, self.heads, self.linked = me, list(heads), list(linked)
        self.bodies, self.owner_type, self.role = list(bodies), owner_type, role
        self.diff = "diff --git a/scripts/x.py b/scripts/x.py\n+fixed\n"
        self.private = "false"
        self.calls: list[list[str]] = []

    def __call__(self, args, timeout=None):
        self.calls.append(args)
        path = args[1] if args[0] == "api" else ""
        if args[:2] == ["pr", "diff"]:
            return self.diff
        if args[:2] == ["pr", "view"]:
            return "".join(json.dumps(i) + "\n" for i in self.linked)
        if args[:2] == ["repo", "view"]:
            return "Own/repo\n"
        if path == "user":
            return self.me + "\n"
        if path == "repos/Own/repo":
            return (self.private if ".private" in args else self.owner_type) + "\n"
        if path.startswith("orgs/"):
            if self.role is None:
                raise dm_refused("HTTP 404")
            return self.role + "\n"
        if path.endswith("/files"):
            return "".join(json.dumps(f) + "\n" for f in self.files)
        if path.endswith("/comments"):
            return "".join(json.dumps(c) + "\n" for c in self.comments)
        if path.endswith("/reviews"):
            return "".join(json.dumps(c) + "\n" for c in self.reviews)
        if "/pulls/" in path:
            head = self.heads.pop(0) if self.heads else self.head
            body = self.bodies.pop(0) if self.bodies else self.body
            return json.dumps({"head": head, "body": body})
        raise AssertionError(args)


def _compose(dm, monkeypatch, tmp_path, capsys, gh, *extra, prose="- DONE: x (`scripts/x.py:4`)\n"):
    monkeypatch.setattr(dm, "_gh", gh)
    pf = tmp_path / "prose.md"
    pf.write_text(prose)
    out = tmp_path / "out.md"
    rc = dm.main(
        [
            "compose",
            "--pr",
            "7",
            "--head",
            HEAD[:12],
            "--verdict",
            "DELIVERS",
            "--scope",
            "CLEAN",
            "--checker",
            "other",
            "--body-file",
            str(pf),
            "--out",
            str(out),
            *extra,
        ]
    )
    return rc, out, capsys.readouterr()


def test_compose_writes_the_comment_and_prints_a_new_comment_command(
    dm, monkeypatch, tmp_path, capsys
):
    rc, out, cap = _compose(dm, monkeypatch, tmp_path, capsys, FakeGh(), "--intended", "read")
    assert rc == 0
    [b] = dm.parse(out.read_text())
    assert b["problem"] is None and b["fields"]["head"] == HEAD
    cmd = cap.out.strip().splitlines()[-1]
    assert shlex.split(cmd) == [
        "gh",
        "api",
        "repos/Own/repo/issues/7/comments",
        "-F",
        f"body=@{out}",
    ]


def test_compose_supersedes_the_owners_latest_marker_with_a_new_comment(
    dm, monkeypatch, tmp_path, capsys
):
    old = _good().replace(HEAD, OTHER)
    gh = FakeGh(
        comments=[
            {
                "id": 11,
                "login": "own",
                "author_association": "OWNER",
                "body": old,
                "stamp": "2026-01-01",
            },
            {
                "id": 12,
                "login": "own",
                "author_association": "OWNER",
                "body": old,
                "stamp": "2026-01-03",
            },
            {
                "id": 13,
                "login": "bot",
                "author_association": "NONE",
                "body": old,
                "stamp": "2026-01-05",
            },
            {
                "id": 14,
                "login": "own",
                "author_association": "OWNER",
                "body": "plain",
                "stamp": "2026-01-09",
            },
        ]
    )
    rc, out, cap = _compose(dm, monkeypatch, tmp_path, capsys, gh, "--intended", "read")
    assert rc == 0
    # A NEW comment (a REST POST), never a PATCH: the push guard denies an agent's
    # comment edit on an open PR, and select() reads the newest owner marker. Not
    # `gh pr comment --body-file`, whose opaque body the review-request gate asks on.
    assert shlex.split(cap.out.strip().splitlines()[-1]) == [
        "gh",
        "api",
        "repos/Own/repo/issues/7/comments",
        "-F",
        f"body=@{out}",
    ]
    assert "supersedes your marker comment 12" in cap.out


def test_compose_never_posts(dm, monkeypatch, tmp_path, capsys):
    gh = FakeGh()
    _compose(dm, monkeypatch, tmp_path, capsys, gh, "--intended", "read")
    for c in gh.calls:
        assert (c[0] in ("api", "repo") or c[:2] == ["pr", "view"]) and "--method" not in c
        assert "comment" not in c


@pytest.mark.parametrize(
    "gh_kw, extra, prose, match",
    [
        ({"head": OTHER}, ["--intended", "read"], None, "head"),
        ({}, [], None, "intended"),
        ({}, ["--intended", "read", "--basis", "issue:1"], None, "basis"),
        ({}, ["--intended", "read"], "- DONE: x (`other.py:4`)\n", "other.py"),
        ({}, ["--intended", "read"], "Fine.\n", "DONE"),
        (
            {},
            ["--intended", "read"],
            "<!-- genesis-delivery-check: x -->\n- DONE: (`scripts/x.py:1`)",
            "<!--",
        ),
        ({}, ["--intended", "read"], "", "empty"),
        ({"body": "Spec: a/b\n"}, [], None, "pointer"),
    ],
)
def test_compose_refusals(dm, monkeypatch, tmp_path, capsys, gh_kw, extra, prose, match):
    kw = {} if prose is None else {"prose": prose}
    rc, out, cap = _compose(dm, monkeypatch, tmp_path, capsys, FakeGh(**gh_kw), *extra, **kw)
    assert rc == 2
    assert match in cap.err
    assert not out.exists()
    assert "gh " not in cap.out


def test_compose_refuses_at_the_rest_file_cap(dm, monkeypatch, tmp_path, capsys):
    files = ["scripts/x.py"] + [f"f{i}.py" for i in range(2999)]
    rc, _, cap = _compose(
        dm, monkeypatch, tmp_path, capsys, FakeGh(files=files), "--intended", "read"
    )
    assert rc == 2 and "3000" in cap.err


def test_compose_refuses_on_a_gh_failure(dm, monkeypatch, tmp_path, capsys):
    def boom(args, timeout=None):
        raise dm.Refused("gh api failed: HTTP 502")

    rc, _, cap = _compose(dm, monkeypatch, tmp_path, capsys, boom, "--intended", "read")
    assert rc == 2 and "502" in cap.err


# ── read: what the PR carries, as the gate will see it ───────────────────────


def test_read_reports_owner_blocks_and_ignores_the_rest(dm, monkeypatch, capsys):
    gh = FakeGh(
        comments=[
            {
                "id": 1,
                "login": "own",
                "author_association": "OWNER",
                "body": _good(),
                "stamp": "s1",
            },
            {"id": 2, "login": "bot", "author_association": "NONE", "body": _good(), "stamp": "s2"},
            {
                "id": 3,
                "login": "own",
                "author_association": "OWNER",
                "body": _block("head=x"),
                "stamp": "s3",
            },
        ],
        reviews=[
            {"id": 9, "login": "own", "author_association": "OWNER", "body": _good(), "stamp": "s4"}
        ],
    )
    monkeypatch.setattr(dm, "_gh", gh)
    assert dm.main(["read", "--pr", "7"]) == 0
    out = capsys.readouterr().out
    assert "comment 1" in out and "at the current head" in out
    assert "comment 2" in out and "not the repo owner" in out
    assert "comment 3" in out and "unusable" in out and "head=x" in out
    assert "review 9" in out and "review body" in out


def test_read_says_when_there_is_no_marker(dm, monkeypatch, capsys):
    monkeypatch.setattr(dm, "_gh", FakeGh())
    assert dm.main(["read", "--pr", "7"]) == 0
    assert "no delivery check recorded" in capsys.readouterr().out


# ── review round (pre-PR architect findings) ─────────────────────────────────


def test_compose_never_supersedes_a_comment_that_only_quotes_the_header(
    dm, monkeypatch, tmp_path, capsys
):
    old = _good().replace(HEAD, OTHER)
    gh = FakeGh(
        comments=[
            {
                "id": 12,
                "login": "own",
                "author_association": "OWNER",
                "body": old,
                "stamp": "2026-01-03",
            },
            {
                "id": 20,
                "login": "own",
                "author_association": "OWNER",
                "body": "Merge question for #7. The marker reads `" + old + "`.",
                "stamp": "2026-01-09",
            },
        ]
    )
    rc, out, cap = _compose(dm, monkeypatch, tmp_path, capsys, gh, "--intended", "read")
    assert rc == 0
    assert "supersedes your marker comment 12;" in cap.out


def test_compose_refuses_when_gh_is_not_the_owner(dm, monkeypatch, tmp_path, capsys):
    rc, out, cap = _compose(
        dm, monkeypatch, tmp_path, capsys, FakeGh(me="someone"), "--intended", "read"
    )
    assert rc == 2 and "not the repo owner" in cap.err and not out.exists()


def test_compose_refuses_when_the_head_moves_mid_compose(dm, monkeypatch, tmp_path, capsys):
    rc, out, cap = _compose(
        dm, monkeypatch, tmp_path, capsys, FakeGh(heads=[HEAD, OTHER]), "--intended", "read"
    )
    assert rc == 2 and "moved" in cap.err and not out.exists()


def test_compose_accepts_an_upper_case_head(dm, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(dm, "_gh", FakeGh())
    pf = tmp_path / "p.md"
    pf.write_text("- DONE: x (`scripts/x.py:4`)\n")
    rc = dm.main(
        [
            "compose",
            "--pr",
            "7",
            "--head",
            HEAD[:12].upper(),
            "--verdict",
            "DELIVERS",
            "--scope",
            "CLEAN",
            "--checker",
            "other",
            "--intended",
            "read",
            "--body-file",
            str(pf),
            "--out",
            str(tmp_path / "o.md"),
        ]
    )
    assert rc == 0


def test_compose_reports_a_missing_prose_file(dm, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(dm, "_gh", FakeGh())
    rc = dm.main(
        [
            "compose",
            "--pr",
            "7",
            "--head",
            HEAD,
            "--verdict",
            "DELIVERS",
            "--scope",
            "CLEAN",
            "--checker",
            "other",
            "--intended",
            "read",
            "--body-file",
            str(tmp_path / "none.md"),
        ]
    )
    assert rc == 2 and "prose file" in capsys.readouterr().err


@pytest.mark.parametrize(
    "variant",
    [
        "1. DONE: y (`zzz.py:5`)",
        "+ DONE: y (`zzz.py:5`)",
        "- **DONE**: y (`zzz.py:5`)",
        "> - DONE: y (`zzz.py:5`)",
        "- Done: y (`zzz.py:5`)",
        "* DONE: y (`zzz.py:5`)",
    ],
)
def test_a_done_line_in_another_form_is_refused_not_skipped(dm, variant):
    prose = "- DONE: x (`a.py:1`)\n" + variant + "\n"
    probs = dm.evidence_problems("DELIVERS", prose, {"a.py"})
    assert probs and "- DONE:" in probs[0]


def _link(n, repo="Own/repo"):
    return {"number": n, "repo": repo}


def test_basis_takes_the_issue_github_links_when_no_pointer_line(dm):
    # GitHub links "Fixes #4245." mid-sentence (measured on a real PR); the pointer
    # parser reads whole lines only, so the link is what names the basis.
    body = "Fixes #4245. References #4246.\n"
    assert dm.basis_from_body(body, linked=lambda: [_link(4245)], repo="Own/repo") == "issue:4245"
    # Unlinked, the line still opens with the pointer, so it names the basis.
    assert dm.basis_from_body(body, linked=lambda: [], repo="Own/repo") == "issue:4245"


def test_a_pointer_line_wins_over_the_linked_issue(dm):
    assert (
        dm.basis_from_body("Closes #7\n", linked=lambda: [_link(9)], repo="Own/repo") == "issue:7"
    )


def test_basis_refuses_several_linked_issues(dm):
    with pytest.raises(dm.Refused, match="several issues"):
        dm.basis_from_body(
            "Fixes #1 and fixes #2\n", linked=lambda: [_link(1), _link(2)], repo="Own/repo"
        )


def test_basis_refuses_a_linked_issue_in_another_repo(dm):
    with pytest.raises(dm.Refused, match="another"):
        dm.basis_from_body(
            "Fixes other/repo#3\n", linked=lambda: [_link(3, "other/repo")], repo="Own/repo"
        )


def test_basis_has_no_default_for_the_linked_issues(dm):
    with pytest.raises(TypeError):
        dm.basis_from_body("x", repo="Own/repo")


def test_compose_reads_the_linked_issue_from_github(dm, monkeypatch, tmp_path, capsys):
    gh = FakeGh(body="Fixes #4602 in the sense that it closes it.\n", linked=[_link(4602)])
    rc, out, _ = _compose(dm, monkeypatch, tmp_path, capsys, gh, "--intended", "read")
    assert rc == 0
    [b] = dm.parse(out.read_text())
    assert b["fields"]["basis"] == "issue:4602"


def test_compose_refuses_an_unreadable_linked_issue_row(dm, monkeypatch, tmp_path, capsys):
    gh = FakeGh(body="Fixes #1 inline.\n", linked=[{"number": "1; x", "repo": "Own/repo"}])
    rc, _, cap = _compose(dm, monkeypatch, tmp_path, capsys, gh, "--intended", "read")
    assert rc == 2 and "linked-issue row" in cap.err


# ── review round (pre-PR security findings) ──────────────────────────────────


@pytest.mark.parametrize(
    "leak",
    [
        "at 192.168.1.5",
        "host 10.0.0.2",
        "node 100.101.1.2",
        "in /home/someone/x",
        "see secrets.env",
    ],
)
def test_compose_refuses_private_data_in_the_public_prose(dm, monkeypatch, tmp_path, capsys, leak):
    prose = f"- DONE: x (`scripts/x.py:4`)\nScope: clean, {leak}\n"
    rc, out, cap = _compose(
        dm, monkeypatch, tmp_path, capsys, FakeGh(), "--intended", "read", prose=prose
    )
    assert rc == 2 and "private" in cap.err and not out.exists()


def test_compose_refuses_a_comment_row_without_an_integer_id(dm, monkeypatch, tmp_path, capsys):
    row = {
        "id": "1; rm -rf ~",
        "login": "own",
        "author_association": "OWNER",
        "body": _good(),
        "stamp": "s",
    }
    rc, _, cap = _compose(
        dm, monkeypatch, tmp_path, capsys, FakeGh(comments=[row]), "--intended", "read"
    )
    assert rc == 2 and "unreadable comment row" in cap.err


def test_a_filename_holding_a_newline_cannot_forge_a_changed_file(
    dm, monkeypatch, tmp_path, capsys
):
    gh = FakeGh(files=["x\nscripts/x.py"])
    rc, _, cap = _compose(dm, monkeypatch, tmp_path, capsys, gh, "--intended", "read")
    assert rc == 2 and "does not change" in cap.err


@pytest.mark.parametrize("repo", ["me/x/../..", "me", "me/x y"])
def test_a_malformed_repo_is_refused(dm, monkeypatch, capsys, repo):
    monkeypatch.setattr(dm, "_gh", FakeGh())
    assert dm.main(["read", "--pr", "7", "--repo", repo]) == 2
    assert "OWNER/REPO" in capsys.readouterr().err


def test_read_escapes_control_characters_from_github(dm, monkeypatch, capsys):
    row = {
        "id": 5,
        "login": "evil\x1b[2J",
        "author_association": "NONE",
        "body": _good(),
        "stamp": "s",
    }
    monkeypatch.setattr(dm, "_gh", FakeGh(comments=[row]))
    assert dm.main(["read", "--pr", "7"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\\x1b" in out


def test_a_pointer_line_never_reads_the_linked_issues(dm):
    def boom():
        raise AssertionError("linked issues read although a pointer line decides")

    assert dm.basis_from_body("Closes #7\n", linked=boom, repo="Own/repo") == "issue:7"


def test_a_linked_issue_with_no_repository_is_unreadable(dm, monkeypatch, tmp_path, capsys):
    gh = FakeGh(body="Fixes #1 inline.\n", linked=[{"number": 1, "repo": "/"}])
    rc, _, cap = _compose(dm, monkeypatch, tmp_path, capsys, gh, "--intended", "read")
    assert rc == 2 and "linked-issue row" in cap.err


# ── review round 1 on the PR (one class: inputs read off their source's rule) ──


def test_a_cited_path_may_hold_spaces(dm):
    prose = "- DONE: x (`docs/user guide.md:12`)\n"
    assert dm.evidence_problems("DELIVERS", prose, {"docs/user guide.md"}) == []
    assert dm.evidence_problems("DELIVERS", prose, {"docs/user"})


def test_a_header_over_several_lines_is_unusable(dm):
    text = f"<!-- genesis-delivery-check: head={HEAD} basis=claim\nverdict=DELIVERS scope=CLEAN -->"
    [b] = dm.parse(text)
    assert b["fields"] is None and "one line" in b["problem"]


@pytest.mark.parametrize(
    "body",
    [
        "This refs #12 for the rest.\n",
        "Depends on #3; refs #12 as well.\n",
        "It is part of #12, the first step.\n",
    ],
)
def test_an_unlinked_issue_named_mid_sentence_refuses(dm, body):
    with pytest.raises(dm.Refused, match="inside a sentence"):
        dm.basis_from_body(body, linked=lambda: [], repo="Own/repo")


@pytest.mark.parametrize(
    "body",
    ["Example:\n\n```\nFixes #9\n```\n", "Text.\n<!-- refs #9 for later -->\n"],
)
def test_hidden_examples_never_block_a_claim(dm, body):
    assert dm.basis_from_body(body, linked=lambda: [], repo="Own/repo") == "claim"


def test_compose_refuses_when_the_basis_changes_mid_compose(dm, monkeypatch, tmp_path, capsys):
    gh = FakeGh(bodies=["Closes #4602\n", "Closes #4603\n"])
    rc, out, cap = _compose(dm, monkeypatch, tmp_path, capsys, gh, "--intended", "read")
    assert rc == 2 and "basis changed" in cap.err and not out.exists()


def test_read_flags_a_basis_edited_since_the_check(dm, monkeypatch, capsys):
    row = {"id": 1, "login": "own", "author_association": "OWNER", "body": _good(), "stamp": "s"}
    monkeypatch.setattr(dm, "_gh", FakeGh(body="Closes #9\n", comments=[row]))
    assert dm.main(["read", "--pr", "7"]) == 0
    out = capsys.readouterr().out
    assert "basis is now issue:9" in out


def test_an_org_owned_repo_refuses_with_the_reason(dm, monkeypatch, tmp_path, capsys):
    gh = FakeGh(me="someone", owner_type="Organization")
    rc, out, cap = _compose(dm, monkeypatch, tmp_path, capsys, gh, "--intended", "read")
    assert rc == 2 and "organization" in cap.err and not out.exists()


@pytest.mark.parametrize(
    "body, basis",
    [
        ("Part of #4677. The diff is 42 counted lines.\n", "issue:4677"),
        ("Intro.\n\n- Refs #12 and more text\n", "issue:12"),
        ("**Fixes #7** and also tidies.\n", "issue:7"),
        ("Text mentions refs #3 later.\nPart of #9: the main work.\n", "issue:9"),
    ],
)
def test_a_line_opening_with_a_pointer_names_the_basis(dm, body, basis):
    assert dm.basis_from_body(body, linked=lambda: [], repo="Own/repo") == basis


def test_github_links_win_over_a_line_opener(dm):
    body = "Part of #9. More.\n"
    assert dm.basis_from_body(body, linked=lambda: [_link(4)], repo="Own/repo") == "issue:4"


@pytest.mark.parametrize(
    "body",
    [
        "This is _Refs #14_ inline.\n",
        "See it: refs owner/repo#77 more.\n",
        "Ok, closes https://github.com/o/r/issues/5 now.\n",
        "Text Closes: #12 here.\n",
    ],
)
def test_the_refusal_net_takes_every_way_a_body_names_an_issue(dm, body):
    with pytest.raises(dm.Refused, match="inside a sentence"):
        dm.basis_from_body(body, linked=lambda: [], repo="Own/repo")


@pytest.mark.parametrize(
    "body, basis",
    [
        ("1. Fixes #3 and more\n", "issue:3"),
        ("> Fixes #1 quoted\n", "issue:1"),
        ("   Refs #4 three spaces\n", "issue:4"),
    ],
)
def test_the_line_opener_follows_the_pointer_parsers_line_rules(dm, body, basis):
    assert dm.basis_from_body(body, linked=lambda: [], repo="Own/repo") == basis


@pytest.mark.parametrize(
    "body",
    [
        "Context.\n\n    Fixes #8 (an indented code example)\n",
        "Fixes #12abc and more\n",
        "Fixes #0 and more\n",
        "Clo\u017fes #9 and more\n",
    ],
)
def test_the_line_opener_rejects_what_the_parser_would(dm, body):
    try:
        got = dm.basis_from_body(body, linked=lambda: [], repo="Own/repo")
    except dm.Refused:
        return
    assert not got.startswith("issue:"), got


def test_a_header_with_a_unicode_line_separator_is_unusable(dm):
    [b] = dm.parse(
        f"<!-- genesis-delivery-check: head={HEAD}\u2028basis=claim verdict=DELIVERS scope=CLEAN -->"
    )
    assert b["fields"] is None and "one line" in b["problem"]


def test_read_warns_on_each_marker_when_the_basis_cannot_be_derived(dm, monkeypatch, capsys):
    row = {"id": 1, "login": "own", "author_association": "OWNER", "body": _good(), "stamp": "s"}
    monkeypatch.setattr(dm, "_gh", FakeGh(body="Spec: a/b\n", comments=[row]))
    assert dm.main(["read", "--pr", "7"]) == 0
    assert "cannot be derived now" in capsys.readouterr().out


# ── the shadow second reader ─────────────────────────────────────────────────

_ASKED = 'Asked: "the gate must bind HEAD"\n- DONE: x (`scripts/x.py:4`)\n'


def _reader(tmp_path, body):
    script = tmp_path / "reader.py"
    script.write_text(body)
    return script


def _configure(monkeypatch, tmp_path, cmd):
    home = tmp_path / "gh_home"
    (home / "config").mkdir(parents=True)
    (home / "config" / "genesis.yaml").write_text(
        "delivery:\n  second_reader_cmd: " + json.dumps(cmd) + "\n"
        if cmd is not None
        else "other: 1\n"
    )
    monkeypatch.setenv("GENESIS_HOME", str(home))
    return home / "delivery" / "shadow.jsonl"


_ECHO = (
    "import json, sys\n"
    "d = json.load(sys.stdin)\n"
    "assert d['form'] == 'F1' and 'the gate must bind HEAD' in d['question'] and '+fixed' in d['diff']\n"
    "print(json.dumps({'p': 0.4242, 'model': 'test-model-1'}))\n"
)


def test_second_reader_logs_the_score_and_never_prints_it(dm, monkeypatch, tmp_path, capsys):
    log = _configure(monkeypatch, tmp_path, [sys.executable, str(_reader(tmp_path, _ECHO))])
    rc, out, cap = _compose(
        dm,
        monkeypatch,
        tmp_path,
        capsys,
        FakeGh(),
        "--intended",
        "read",
        "--second-reader",
        prose=_ASKED,
    )
    assert rc == 0 and out.exists()
    [rec] = [json.loads(x) for x in log.read_text().splitlines()]
    assert rec["p"] == 0.4242 and rec["form"] == "F1" and rec["model"] == "test-model-1"
    assert (
        rec["pr"] == 7
        and rec["head"] == HEAD
        and rec["verdict"] == "DELIVERS"
        and rec["error"] is None
    )
    assert "0.42" not in cap.out + cap.err and "recorded" in cap.err
    assert "0.42" not in out.read_text()


def test_no_configured_command_runs_nothing(dm, monkeypatch, tmp_path, capsys):
    log = _configure(monkeypatch, tmp_path, None)
    rc, _, cap = _compose(
        dm,
        monkeypatch,
        tmp_path,
        capsys,
        FakeGh(),
        "--intended",
        "read",
        "--second-reader",
        prose=_ASKED,
    )
    assert rc == 0 and "none configured" in cap.err and not log.exists()


def test_without_the_flag_nothing_runs(dm, monkeypatch, tmp_path, capsys):
    log = _configure(monkeypatch, tmp_path, [sys.executable, str(_reader(tmp_path, _ECHO))])
    rc, _, cap = _compose(
        dm, monkeypatch, tmp_path, capsys, FakeGh(), "--intended", "read", prose=_ASKED
    )
    assert rc == 0 and not log.exists() and "second reader" not in cap.err


@pytest.mark.parametrize(
    "body, prose, why",
    [
        (f"Ledger: {HEX32}\n", _ASKED, "not a public issue"),
        ("Spec: some-spec\n", _ASKED, "not a public issue"),
        ("Closes #4602\n", "- DONE: x (`scripts/x.py:4`)\n", "no Asked"),
    ],
)
def test_private_bases_and_missing_fix_text_are_skipped(
    dm, monkeypatch, tmp_path, capsys, body, prose, why
):
    log = _configure(
        monkeypatch, tmp_path, [sys.executable, "-c", "raise SystemExit('must not run')"]
    )
    extra = ["--intended", "read"] if body.startswith("Closes") else []
    rc, _, cap = _compose(
        dm, monkeypatch, tmp_path, capsys, FakeGh(body=body), *extra, "--second-reader", prose=prose
    )
    assert rc == 0 and why in cap.err and not log.exists()


@pytest.mark.parametrize(
    "reader",
    [
        "import sys; sys.exit(3)",
        "print('not json')",
        "import json; print(json.dumps({'p': 1.5}))",
        "import json; print(json.dumps({'p': True}))",
        "import json; print(json.dumps(['p', 0.5]))",
    ],
)
def test_a_failing_reader_is_logged_and_never_stops_compose(
    dm, monkeypatch, tmp_path, capsys, reader
):
    log = _configure(monkeypatch, tmp_path, [sys.executable, str(_reader(tmp_path, reader))])
    rc, out, cap = _compose(
        dm,
        monkeypatch,
        tmp_path,
        capsys,
        FakeGh(),
        "--intended",
        "read",
        "--second-reader",
        prose=_ASKED,
    )
    assert rc == 0 and out.exists() and "failed" in cap.err
    [rec] = [json.loads(x) for x in log.read_text().splitlines()]
    assert rec["p"] is None and rec["error"]


def test_an_oversized_diff_is_not_sent(dm, monkeypatch, tmp_path, capsys):
    log = _configure(
        monkeypatch, tmp_path, [sys.executable, "-c", "raise SystemExit('must not run')"]
    )
    gh = FakeGh()
    gh.diff = "x" * (dm._DIFF_CAP + 1)
    rc, _, cap = _compose(
        dm, monkeypatch, tmp_path, capsys, gh, "--intended", "read", "--second-reader", prose=_ASKED
    )
    assert rc == 0 and "over" in cap.err
    [rec] = [json.loads(x) for x in log.read_text().splitlines()]
    assert rec["p"] is None and "over" in rec["error"]


@pytest.mark.parametrize(
    "yaml_text, argv0",
    [
        ("delivery:\n  second_reader_cmd: '~/bin/r --x'\n", "/bin/r"),
        ("delivery:\n  second_reader_cmd: [a, b]\n", "a"),
        ("delivery:\n  second_reader_cmd: 7\n", None),
        ("delivery: 3\n", None),
        ("- a\n- b\n", None),
        (": : :\n", None),
    ],
)
def test_second_reader_cmd_reads_only_a_string_or_list(dm, monkeypatch, tmp_path, yaml_text, argv0):
    home = tmp_path / "h"
    (home / "config").mkdir(parents=True)
    (home / "config" / "genesis.yaml").write_text(yaml_text)
    monkeypatch.setenv("GENESIS_HOME", str(home))
    got = dm.second_reader_cmd()
    assert (got is None) if argv0 is None else got[0].endswith(argv0)


def test_second_reader_cmd_expands_a_home_path_in_any_argument(dm, monkeypatch, tmp_path):
    home = tmp_path / "h"
    (home / "config").mkdir(parents=True)
    (home / "config" / "genesis.yaml").write_text(
        "delivery:\n  second_reader_cmd: [python3, ~/r.py, x~y]\n"
    )
    monkeypatch.setenv("GENESIS_HOME", str(home))
    got = dm.second_reader_cmd()
    assert (
        got[0] == "python3"
        and not got[1].startswith("~")
        and got[1].endswith("/r.py")
        and got[2] == "x~y"
    )


@pytest.mark.parametrize(
    "yaml_text",
    [
        "delivery:\n  second_reader_cmd: 'python3 \"unbalanced'\n",
        "delivery:\n  second_reader_cmd: [~nosuchuser_zz9/r.py]\n",
    ],
)
def test_a_malformed_command_reads_as_none_and_never_raises(dm, monkeypatch, tmp_path, yaml_text):
    home = tmp_path / "h"
    (home / "config").mkdir(parents=True)
    (home / "config" / "genesis.yaml").write_text(yaml_text)
    monkeypatch.setenv("GENESIS_HOME", str(home))
    assert dm.second_reader_cmd() is None


def test_a_private_repository_is_never_sent(dm, monkeypatch, tmp_path, capsys):
    log = _configure(
        monkeypatch, tmp_path, [sys.executable, "-c", "raise SystemExit('must not run')"]
    )
    gh = FakeGh()
    gh.private = "true"
    rc, _, cap = _compose(
        dm, monkeypatch, tmp_path, capsys, gh, "--intended", "read", "--second-reader", prose=_ASKED
    )
    assert rc == 0 and "not public" in cap.err and not log.exists()
    assert not any(c[:2] == ["pr", "diff"] for c in gh.calls)


def test_a_head_that_moves_during_the_read_logs_no_score(dm, monkeypatch, tmp_path, capsys):
    log = _configure(monkeypatch, tmp_path, [sys.executable, str(_reader(tmp_path, _ECHO))])
    gh = FakeGh(heads=[HEAD, HEAD, OTHER])  # compose's two reads, then the reader's
    rc, _, cap = _compose(
        dm, monkeypatch, tmp_path, capsys, gh, "--intended", "read", "--second-reader", prose=_ASKED
    )
    assert rc == 0
    [rec] = [json.loads(x) for x in log.read_text().splitlines()]
    assert rec["p"] is None and "moved" in rec["error"]


@pytest.mark.parametrize(
    "reader",
    [
        "import json; print(json.dumps({'p': '0.4242', 'model': 'm'}))",
        "import sys; sys.stderr.write('p=0.4242'); sys.exit(1)",
        "print('{\"p\": 0.4242')",
    ],
)
def test_a_bad_result_never_shows_the_score(dm, monkeypatch, tmp_path, capsys, reader):
    log = _configure(monkeypatch, tmp_path, [sys.executable, str(_reader(tmp_path, reader))])
    rc, out, cap = _compose(
        dm,
        monkeypatch,
        tmp_path,
        capsys,
        FakeGh(),
        "--intended",
        "read",
        "--second-reader",
        prose=_ASKED,
    )
    assert rc == 0 and "failed" in cap.err
    assert "4242" not in cap.out + cap.err + log.read_text() + out.read_text()
    assert "while running the command" not in log.read_text()  # not the catch-all


def test_a_missing_executable_is_logged_not_raised(dm, monkeypatch, tmp_path, capsys):
    log = _configure(monkeypatch, tmp_path, [str(tmp_path / "no-such-reader")])
    rc, _, cap = _compose(
        dm,
        monkeypatch,
        tmp_path,
        capsys,
        FakeGh(),
        "--intended",
        "read",
        "--second-reader",
        prose=_ASKED,
    )
    assert rc == 0 and "failed" in cap.err
    [rec] = [json.loads(x) for x in log.read_text().splitlines()]
    assert rec["p"] is None and rec["error"]


def test_any_unexpected_error_in_the_reader_never_stops_compose(dm, monkeypatch, tmp_path, capsys):
    def boom(**kw):
        raise RecursionError("deep")

    monkeypatch.setattr(dm, "second_reader", boom)
    rc, out, cap = _compose(
        dm,
        monkeypatch,
        tmp_path,
        capsys,
        FakeGh(),
        "--intended",
        "read",
        "--second-reader",
        prose=_ASKED,
    )
    assert rc == 0 and out.exists() and "RecursionError" in cap.err


def test_every_gh_read_the_reader_makes_is_bounded(dm, monkeypatch, tmp_path, capsys):
    _configure(monkeypatch, tmp_path, [sys.executable, str(_reader(tmp_path, _ECHO))])
    gh = FakeGh()
    seen = []
    inner = gh.__call__

    def spy(args, timeout=None):
        seen.append((args[:2], timeout))
        return inner(args, timeout=timeout)

    rc, _, _ = _compose(
        dm,
        monkeypatch,
        tmp_path,
        capsys,
        spy,
        "--intended",
        "read",
        "--second-reader",
        prose=_ASKED,
    )
    assert rc == 0
    # The reader's own reads come after the marker is written: the visibility, the
    # diff and the final head check, each with the reader's bound.
    reader = seen[-3:]
    assert [a for a, _ in reader] == [
        ["api", "repos/Own/repo"],
        ["pr", "diff"],
        ["api", "repos/Own/repo/pulls/7"],
    ]
    assert all(t == dm._GH_TIMEOUT_S for _, t in reader)


def test_a_tilde_genesis_home_is_a_home_path(dm, monkeypatch):
    monkeypatch.setenv("GENESIS_HOME", "~/custom-genesis-home")
    got = dm._genesis_home()
    assert "~" not in str(got) and got == Path.home() / "custom-genesis-home"


def test_an_unknown_user_genesis_home_never_crashes(dm, monkeypatch):
    monkeypatch.setenv("GENESIS_HOME", "~nosuchuser_zz9/h")
    assert str(dm._genesis_home()) == "~nosuchuser_zz9/h"


# ── (aside) and select (spec PR 4; owner ruling 2026-11-17) ──────────────────


def test_a_leading_aside_line_still_opens_the_marker(dm):
    # On a Devin-built PR a comment must start with `(aside)` or it starts a paid
    # Devin session; the marker may sit on the line after it.
    [b] = dm.parse("(aside)\n" + _good())
    assert b["problem"] is None and b["fields"]["verdict"] == "DELIVERS"
    [b] = dm.parse("(aside)\r\n" + _good())
    assert b["problem"] is None


@pytest.mark.parametrize(
    "text",
    [
        "(aside) note\n" + _good(),  # more than `(aside)` on the first line
        "(aside)\n\nprose first\n" + _good(),  # anything between it and the marker
        "(aside)\n(aside)\n" + _good(),  # only one
        "Aside\n" + _good(),
    ],
)
def test_only_a_bare_aside_line_may_precede_the_marker(dm, text):
    assert dm.parse(text) == []


def test_compose_aside_writes_the_aside_line_first(dm, monkeypatch, tmp_path, capsys):
    rc, out, _ = _compose(
        dm, monkeypatch, tmp_path, capsys, FakeGh(), "--intended", "read", "--aside"
    )
    assert rc == 0
    text = out.read_text()
    assert text.startswith("(aside)\n<!-- genesis-delivery-check: ")
    [b] = dm.parse(text)
    assert b["problem"] is None


def _row(cid, body, stamp, login="own", assoc="OWNER"):
    return {"id": cid, "login": login, "author_association": assoc, "body": body, "stamp": stamp}


def _at(head, verdict="DELIVERS"):
    return _block(
        f"head={head} basis=issue:4602 verdict={verdict} scope=CLEAN intended=read checker=other"
    )


def test_select_prefers_a_block_at_the_current_head(dm):
    rows = [_row(1, _at(HEAD), "2026-01-01"), _row(2, _at(OTHER), "2026-01-05")]
    got = dm.select(rows, [], "own", HEAD)
    assert got["governing"]["id"] == 1 and got["governing"]["fields"]["head"] == HEAD


def test_select_falls_back_to_the_latest_block_and_breaks_ties_by_id(dm):
    third = "1" * 40
    rows = [
        _row(1, _at(OTHER), "2026-01-05"),
        _row(7, _at(third), "2026-01-05"),
        _row(3, _at(OTHER), "2026-01-01"),
    ]
    assert dm.select(rows, [], "own", HEAD)["governing"]["id"] == 7


def test_select_at_one_head_takes_the_latest_stamp(dm):
    rows = [_row(1, _at(HEAD, "PARTIAL"), "2026-01-01"), _row(2, _at(HEAD), "2026-01-03")]
    assert dm.select(rows, [], "own", HEAD)["governing"]["fields"]["verdict"] == "DELIVERS"


def test_select_ignores_non_owner_and_review_blocks_with_notes(dm):
    rows = [_row(1, _at(HEAD), "2026-01-01", login="bot", assoc="NONE")]
    reviews = [_row(2, _at(HEAD), "2026-01-02")]
    got = dm.select(rows, reviews, "own", HEAD)
    assert got["governing"] is None
    assert any("not the repo owner" in n for n in got["notes"])
    assert any("review body" in n for n in got["notes"])


def test_a_later_unusable_owner_block_is_reported_never_hidden(dm):
    rows = [
        _row(1, _at(HEAD), "2026-01-01"),
        _row(2, _block("head=x verdict=DOES-NOT"), "2026-01-02"),
    ]
    got = dm.select(rows, [], "own", HEAD)
    assert got["governing"]["id"] == 1
    [late] = got["later_unusable"]
    assert late["id"] == 2 and "head=x" in late["problem"]


def test_an_earlier_unusable_owner_block_does_not_shadow_a_valid_one(dm):
    rows = [_row(1, _block("head=x"), "2026-01-01"), _row(2, _at(HEAD), "2026-01-02")]
    got = dm.select(rows, [], "own", HEAD)
    assert got["governing"]["id"] == 2 and got["later_unusable"] == []


def test_read_marks_the_block_that_governs(dm, monkeypatch, capsys):
    gh = FakeGh(comments=[_row(1, _at(OTHER), "2026-01-01"), _row(2, _at(HEAD), "2026-01-02")])
    monkeypatch.setattr(dm, "_gh", gh)
    assert dm.main(["read", "--pr", "7"]) == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.startswith("- ")]
    assert [ln for ln in lines if "governs" in ln] == [ln for ln in lines if "comment 2" in ln]


@pytest.mark.parametrize(
    "text",
    [
        "(aside)\n\n" + _good(),  # a blank line between
        "  (aside)\n" + _good(),  # not the first thing
        "  \n(aside)\n" + _good(),  # not on the first line
        "(Aside)\n" + _good(),  # Devin reads the exact word
        "(aside)\n " + _good(),  # the marker indented
    ],
)
def test_a_near_miss_aside_is_reported_as_unusable(dm, text):
    [b] = dm.parse(text)
    assert b["fields"] is None and "(aside)" in b["problem"]


def test_trailing_spaces_after_aside_are_accepted(dm):
    [b] = dm.parse("(aside) \t\n" + _good())
    assert b["problem"] is None


def test_a_re_read_keeps_the_aside_line_it_replaces(dm, monkeypatch, tmp_path, capsys):
    gh = FakeGh(comments=[_row(12, "(aside)\n" + _good(), "2026-01-05")])
    rc, out, cap = _compose(dm, monkeypatch, tmp_path, capsys, gh, "--intended", "read")
    assert rc == 0 and out.read_text().startswith("(aside)\n")
    assert "marker comment 12;" in cap.out


def test_compose_replaces_the_comment_select_would_pick_on_a_tie(dm, monkeypatch, tmp_path, capsys):
    gh = FakeGh(comments=[_row(12, _good(), "2026-01-05"), _row(15, _good(), "2026-01-05")])
    rc, _, cap = _compose(dm, monkeypatch, tmp_path, capsys, gh, "--intended", "read")
    assert rc == 0 and "marker comment 15;" in cap.out


def test_a_re_read_posted_as_a_new_comment_governs(dm):
    """compose posts a re-read as a new comment, so select must take the newest
    owner marker at head over the older one it supersedes."""
    rows = [_row(12, _at(HEAD), "2026-01-03"), _row(30, _at(HEAD), "2026-01-09")]
    assert dm.select(rows, [], "own", HEAD)["governing"]["id"] == 30


def test_select_skips_a_row_without_an_integer_id(dm):
    rows = [
        _row("IC_kwDO", _at(HEAD), "2026-01-09"),
        _row(True, _at(HEAD), "2026-01-09"),
        _row(3, _at(HEAD), "2026-01-01"),
    ]
    got = dm.select(rows, [], "own", HEAD)
    assert got["governing"]["id"] == 3
    assert sum("not a GitHub integer id" in n for n in got["notes"]) == 2
