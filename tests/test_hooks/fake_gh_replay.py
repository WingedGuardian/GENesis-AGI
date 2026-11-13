"""The fake ``gh`` for ``test_merge_gate_server_replay.py``: a script, not a test module.

That file's shim runs ``python <this file> <argv>`` first on ``PATH``, so every ``gh``
call the merge gate makes lands in ``main``. It routes on argv, applies the caller's
``--jq`` with the real ``jq`` (``gh --jq`` prints strings raw and everything else as
compact JSON, measured 2026-11-08 against gh 2.101.0), and FAILS LOUDLY on any argv it
does not know: exit 97 and a ``FAKE-GH-UNROUTED`` line, which every test asserts is
absent. GraphQL is the exception to argv routing: a query is answered only when its
whitespace-normalized document is byte-equal to one of ``GRAPHQL_DOCUMENTS``, the
gate's own queries recorded here. Each call is appended to the JSONL log named by
``LOG_ENV``; the recorded PR state is read from the JSON file named by ``STATE_ENV``.

It imports nothing from the repository, so it runs the same from a checkout and from
the mutation sweep's copy of one (the shim names it by the test file's ``__file__``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qsl

#: The distinctive line the fake prints on an argv it does not know.
UNROUTED = "FAKE-GH-UNROUTED"
STATE_ENV = "SERVER_REPLAY_GH_STATE"
LOG_ENV = "SERVER_REPLAY_GH_LOG"


class _Unrouted(Exception):
    pass


def _opts(args: list[str], flags_with_value: set[str]) -> tuple[list[str], dict[str, list[str]]]:
    """Positional args and ``{flag: [values]}``; a flag not in the set is a bare switch."""
    pos: list[str] = []
    opts: dict[str, list[str]] = {}
    i = 0
    while i < len(args):
        tok = args[i]
        if tok.startswith("-"):
            if tok in flags_with_value:
                if i + 1 >= len(args):
                    raise _Unrouted(f"{tok} has no value")
                opts.setdefault(tok, []).append(args[i + 1])
                i += 2
                continue
            opts.setdefault(tok, []).append("")
        else:
            pos.append(tok)
        i += 1
    return pos, opts


def _one(opts: dict[str, list[str]], flag: str) -> str | None:
    vals = opts.get(flag)
    if vals is None:
        return None
    if len(vals) != 1:
        raise _Unrouted(f"{flag} given {len(vals)} times")
    return vals[0]


def _allow_only(opts: dict[str, list[str]], allowed: set[str]) -> None:
    if extra := set(opts) - allowed:
        raise _Unrouted(f"unexpected flag(s) {sorted(extra)}")


def _jq(expr: str | None, doc: object) -> tuple[int, str, str]:
    """``gh --jq``: strings raw, everything else compact JSON, one result per line."""
    text = json.dumps(doc)
    if expr is None:
        return 0, text + "\n", ""
    jq = shutil.which("jq")
    if jq is None:
        raise _Unrouted("no jq binary on PATH to apply --jq (the server replay needs one)")
    proc = subprocess.run([jq, "-r", "-c", expr], input=text, capture_output=True, text=True)
    if proc.returncode != 0:
        # gh exits 1 when its jq expression fails.
        return 1, "", f"jq: {proc.stderr.strip()}\n"
    return 0, proc.stdout, ""


def _answer(value: object, expr: str | None) -> tuple[int, str, str]:
    """A recorded failure is ``{"__gh_error__": "<stderr>"}``; anything else is data."""
    if isinstance(value, dict) and "__gh_error__" in value:
        return 1, "", str(value["__gh_error__"]) + "\n"
    return _jq(expr, value)


def _check_repo(opts: dict[str, list[str]], state: dict) -> None:
    if _one(opts, "--repo") != state["repo"]:
        raise _Unrouted(f"--repo is not {state['repo']}")


def _pr_view(args: list[str], state: dict) -> tuple[int, str, str]:
    pos, opts = _opts(args, {"--repo", "--json", "--jq"})
    _allow_only(opts, {"--repo", "--json", "--jq"})
    _check_repo(opts, state)
    if pos != [state["pr"]]:
        raise _Unrouted(f"pr view of {pos}, not PR {state['pr']}")
    fields = (_one(opts, "--json") or "").split(",")
    pr = state["pr_view"]
    if not fields or any(f not in pr for f in fields):
        raise _Unrouted(f"no recorded pr field(s) {[f for f in fields if f not in pr]}")
    return _answer({f: pr[f] for f in fields}, _one(opts, "--jq"))


def _pr_list(args: list[str], state: dict) -> tuple[int, str, str]:
    pos, opts = _opts(args, {"--repo", "--state", "--label", "--limit", "--json", "--jq"})
    _allow_only(opts, {"--repo", "--state", "--label", "--limit", "--json", "--jq"})
    _check_repo(opts, state)
    label = _one(opts, "--label")
    if pos or _one(opts, "--state") != "all" or label not in state["pr_list"]:
        raise _Unrouted(f"pr list for label {label!r}")
    fields = (_one(opts, "--json") or "").split(",")
    rows = [{f: row[f] for f in fields} for row in state["pr_list"][label]]
    return _answer(rows, _one(opts, "--jq"))


def _repo_view(args: list[str], state: dict) -> tuple[int, str, str]:
    pos, opts = _opts(args, {"--json", "--jq"})
    _allow_only(opts, {"--json", "--jq"})
    # `gh repo view` takes the repository as a positional and has no --repo. With none
    # it asks the cwd's git remote, which the fake cannot answer for.
    if pos != [state["repo"]]:
        raise _Unrouted(f"repo view of {pos}, not {state['repo']}")
    fields = (_one(opts, "--json") or "").split(",")
    repo = state["repo_view"]
    if any(f not in repo for f in fields):
        raise _Unrouted(f"no recorded repo field(s) in {fields}")
    return _answer({f: repo[f] for f in fields}, _one(opts, "--jq"))


def _run_cmd(args: list[str], state: dict) -> tuple[int, str, str]:
    sub, rest = args[0], args[1:]
    if sub == "list":
        flags = {"--repo", "--commit", "--branch", "--workflow", "--event", "--limit", "--json"}
        pos, opts = _opts(rest, flags)
        _allow_only(opts, flags)
        _check_repo(opts, state)
        key = " ".join(
            f"{flag}={_one(opts, flag)}"
            for flag in ("--commit", "--branch", "--workflow", "--event")
            if flag in opts
        )
        if pos or key not in state["run_list"]:
            raise _Unrouted(f"run list {key!r}")
        recorded = state["run_list"][key]
        if isinstance(recorded, dict):  # a recorded failure
            return _answer(recorded, None)
        fields = (_one(opts, "--json") or "").split(",")
        limit = int(_one(opts, "--limit") or "20")
        rows = [{f: row[f] for f in fields} for row in recorded[:limit]]
        return _answer(rows, None)
    if sub == "view":
        pos, opts = _opts(rest, {"--repo", "--json", "--jq"})
        _allow_only(opts, {"--repo", "--json", "--jq"})
        _check_repo(opts, state)
        if len(pos) != 1 or pos[0] not in state["run_view"] or _one(opts, "--json") != "jobs":
            raise _Unrouted(f"run view {pos}")
        return _answer({"jobs": state["run_view"][pos[0]]}, _one(opts, "--jq"))
    raise _Unrouted(f"run {sub}")


def _normalize(document: str) -> str:
    """A GraphQL document with every whitespace run collapsed to one space.

    Only GraphQL's own whitespace (space, tab, CR, LF) counts. ``str.split()`` would
    also fold a no-break space or a Unicode separator, which GitHub would not."""
    return " ".join(re.split(r"[ \t\r\n]+", document.strip(" \t\r\n")))


#: Every GraphQL document the gate sends, recorded whitespace-normalized. Only a
#: document byte-equal to one of these is answered, so a mutation, a changed
#: selection set or a phrase smuggled into a comment is UNROUTED by construction.
#: Recorded by hand, never imported: the corpus mutates the files these come from,
#: and a mutant's query must be refused. When a gate query really changes,
#: ``test_every_stored_graphql_document_matches_the_live_query`` names the one to
#: re-record. Sources: ``base_ref`` is the local ``query`` in
#: ``git_push_guard._pr_base_sha``; ``codex_signal`` is ``_CODEX_SIGNAL_QUERY``;
#: ``outside_pr`` is ``_OUTSIDE_PR_QUERY``; the two ``review_budget`` documents are
#: ``review_budget._graphql_query`` of the connections in ``_REVIEW_BUDGET_READS``.
GRAPHQL_DOCUMENTS = {
    "base_ref": (
        "query($owner:String!,$name:String!,$ref:String!){repository(owner:$owner,nam"
        "e:$name){ref(qualifiedName:$ref){target{oid}}}}"
    ),
    "codex_signal": (
        "query($owner: String!, $name: String!, $number: Int!) { repository(owner:"
        " $owner, name: $name) { pullRequest(number: $number) { comments(last: 100) {"
        " totalCount nodes { body author { login __typename } editor { login }"
        " userContentEdits(first: 50) { totalCount nodes { editor { login } deletedAt"
        " } } } } timelineItems(itemTypes: [HEAD_REF_FORCE_PUSHED_EVENT,"
        " HEAD_REF_DELETED_EVENT, HEAD_REF_RESTORED_EVENT, BASE_REF_CHANGED_EVENT],"
        " first: 1) { filteredCount nodes { __typename } } } } }"
    ),
    "outside_pr": (
        "query($owner:String!,$name:String!,$num:Int!){repository(owner:$owner,name:$"
        "name){pullRequest(number:$num){headRefOid"
        " isCrossRepository authorAssociation author{__typename login}"
        " userContentEdits(first:100){totalCount nodes{editor{__typename login}}}"
        " timelineItems(itemTypes:[RENAMED_TITLE_EVENT],first:100){filteredCount"
        " nodes{... on RenamedTitleEvent{actor{login}}}} forcePushes:"
        " timelineItems(itemTypes:[HEAD_REF_FORCE_PUSHED_EVENT],first:1){filteredCoun"
        "t}}}}"
    ),
    "review_budget": (
        "query($owner: String!, $name: String!, $number: Int!, $after_reviews:"
        " String, $after_comments: String, $after_files: String, $after_commits:"
        " String) { repository(owner: $owner, name: $name) { pullRequest(number:"
        " $number) { headRefOid body reviews(first: 100, after: $after_reviews) {"
        " pageInfo { hasNextPage endCursor } nodes { fullDatabaseId state submittedAt"
        " body author { login __typename } commit { oid } comments(first: 100) {"
        " pageInfo { hasNextPage } nodes { fullDatabaseId replyTo { id } body } } } }"
        " comments(first: 100, after: $after_comments) { pageInfo { hasNextPage"
        " endCursor } nodes { fullDatabaseId body createdAt updatedAt author { login"
        " __typename } } } files(first: 100, after: $after_files) { pageInfo {"
        " hasNextPage endCursor } nodes { path changeType } } commits(first: 100,"
        " after: $after_commits) { pageInfo { hasNextPage endCursor } nodes { commit"
        " { oid parents(first: 100) { totalCount nodes { oid } } } } } } } }"
    ),
    "review_budget_reread": (
        "query($owner: String!, $name: String!, $number: Int!, $after_reviews:"
        " String, $after_comments: String, $after_commits: String) {"
        " repository(owner: $owner, name: $name) { pullRequest(number: $number) {"
        " headRefOid body reviews(first: 100, after: $after_reviews) { pageInfo {"
        " hasNextPage endCursor } nodes { fullDatabaseId state submittedAt body"
        " author { login __typename } commit { oid } comments(first: 100) { pageInfo"
        " { hasNextPage } nodes { fullDatabaseId replyTo { id } body } } } }"
        " comments(first: 100, after: $after_comments) { pageInfo { hasNextPage"
        " endCursor } nodes { fullDatabaseId body createdAt updatedAt author { login"
        " __typename } } } commits(first: 100, after: $after_commits) { pageInfo {"
        " hasNextPage endCursor } nodes { commit { oid parents(first: 100) {"
        " totalCount nodes { oid } } } } } } } }"
    ),
}

#: The connections each review_budget document selects, in its order: the first
#: snapshot reads all four, the re-read after it only the three that can move.
#: The answer carries exactly these, as GitHub's would.
REVIEW_BUDGET_READS = {
    "review_budget": ("reviews", "comments", "files", "commits"),
    "review_budget_reread": ("reviews", "comments", "commits"),
}

_BY_DOCUMENT = {_normalize(doc): name for name, doc in GRAPHQL_DOCUMENTS.items()}

#: The variables each query carries beside ``query``, with the flag its call site
#: sends each one by: ``-f`` is a raw string, ``-F`` is type-coerced, which an
#: ``Int!`` needs. Any other variable or flag is refused, so a value the fake never
#: checks cannot change the answer unseen, and a number sent as a string is not
#: answered as if GitHub had accepted it.
_GRAPHQL_VARIABLES = {
    "base_ref": {"owner": "-f", "name": "-f", "ref": "-f"},
    "codex_signal": {"owner": "-F", "name": "-F", "number": "-F"},
    "outside_pr": {"owner": "-f", "name": "-f", "num": "-F"},
    "review_budget": {"owner": "-f", "name": "-f", "number": "-F"},
    "review_budget_reread": {"owner": "-f", "name": "-f", "number": "-F"},
}


#: The REST ``files[].status`` values and the GraphQL ``PatchStatus`` GitHub reports
#: for the same file. The GraphQL node carries no previous path, so a reader learns
#: of a rename only from ``RENAMED``/``COPIED`` and goes back to REST for the path.
_PATCH_STATUS = {
    "added": "ADDED",
    "removed": "DELETED",
    "modified": "MODIFIED",
    "renamed": "RENAMED",
    "copied": "COPIED",
    "changed": "CHANGED",
}


def _change_type(record: dict) -> str:
    status = record.get("status")
    if status not in _PATCH_STATUS:
        raise _Unrouted(f"recorded file {record.get('filename')!r} has status {status!r}")
    return _PATCH_STATUS[status]


def _graphql(opts: dict[str, list[str]], state: dict) -> tuple[int, str, str]:
    # gh sends a graphql query as a POST; any other method is not the read recorded.
    if _one(opts, "-X") not in (None, "POST"):
        raise _Unrouted(f"api graphql -X {_one(opts, '-X')}")
    if "--paginate" in opts:
        raise _Unrouted("api graphql --paginate: every recorded page is the last")
    fields: dict[str, str] = {}
    sent_by: dict[str, str] = {}
    for flag in ("-f", "-F"):
        for item in opts.get(flag, []):
            key, sep, value = item.partition("=")
            if not sep or key in fields:
                raise _Unrouted(f"graphql field {item!r}")
            fields[key] = value
            sent_by[key] = flag
    if sent_by.pop("query", None) != "-f":
        raise _Unrouted("graphql query not sent as one raw -f field")
    query = fields.pop("query")
    route = _BY_DOCUMENT.get(_normalize(query))
    if route is None:
        raise _Unrouted(f"graphql document is not a stored gate query: {query[:120]!r}")
    if any(k.startswith("after_") for k in fields):
        raise _Unrouted("graphql asked for a second page; every recorded page is the last")
    if sent_by != _GRAPHQL_VARIABLES[route]:
        raise _Unrouted(f"graphql {route} variables by flag {sorted(sent_by.items())}")
    owner, name = state["repo"].split("/")
    if fields.get("owner") != owner or fields.get("name") != name:
        raise _Unrouted("graphql owner/name is not the recorded repository")
    number = fields.get("number", fields.get("num"))
    if route != "base_ref" and number != state["pr"]:
        raise _Unrouted(f"graphql for PR {number}, not {state['pr']}")
    if route == "base_ref" and fields.get("ref") != f"refs/heads/{state['pr_view']['baseRefName']}":
        raise _Unrouted(f"graphql ref {fields.get('ref')!r}")
    if route in REVIEW_BUDGET_READS:
        answer = _review_budget_answer(state, REVIEW_BUDGET_READS[route])
        return _answer(answer, _one(opts, "--jq"))
    if route not in state["graphql"]:
        raise _Unrouted(f"no recorded answer for the {route} query")
    return _answer(state["graphql"][route], _one(opts, "--jq"))


def _graphql_author(user: dict) -> dict:
    """REST ``user`` as GraphQL names it: a bot by its bare slug."""
    login = user["login"]
    if user.get("type") == "Bot":
        return {"login": login.removesuffix("[bot]"), "__typename": "Bot"}
    return {"login": login, "__typename": "User"}


def _review_budget_answer(state: dict, connections: tuple[str, ...]) -> dict:
    """The ``rounds`` row's one GraphQL page, derived from the same REST records the
    rows read, so a fixture states each fact once. It carries only the
    ``connections`` the document selected, as GitHub's answer would."""
    api, pr = state["api"], state["pr"]
    last = {"hasNextPage": False, "endCursor": None}
    inline = api[f"pulls/{pr}/comments"]
    reviews = [
        {
            "fullDatabaseId": str(r["id"]),
            "state": r["state"],
            "submittedAt": r["submitted_at"],
            "body": r["body"],
            "author": _graphql_author(r["user"]),
            "commit": {"oid": r["commit_id"]},
            "comments": {
                "pageInfo": {"hasNextPage": False},
                "nodes": [
                    {
                        "fullDatabaseId": str(c["id"]),
                        "replyTo": {"id": str(c["in_reply_to_id"])}
                        if c.get("in_reply_to_id")
                        else None,
                        "body": c["body"],
                    }
                    for c in inline
                    if c.get("pull_request_review_id") == r["id"]
                ],
            },
        }
        for r in api[f"pulls/{pr}/reviews"]
    ]
    comments = [
        {
            "fullDatabaseId": str(c["id"]),
            "body": c["body"],
            "createdAt": c["created_at"],
            "updatedAt": c["updated_at"],
            "author": _graphql_author(c["user"]),
        }
        for c in api[f"issues/{pr}/comments"]
    ]
    files = (
        [{"path": f["filename"], "changeType": _change_type(f)} for f in api[f"pulls/{pr}/files"]]
        if "files" in connections
        else []
    )
    commits = [
        {
            "commit": {
                "oid": c["sha"],
                "parents": {
                    "totalCount": len(c["parents"]),
                    "nodes": [{"oid": p["sha"]} for p in c["parents"]],
                },
            }
        }
        for c in api[f"pulls/{pr}/commits"]
    ]
    nodes = {"reviews": reviews, "comments": comments, "files": files, "commits": commits}
    pull = {"headRefOid": state["pr_view"]["headRefOid"], "body": state["pr_view"]["body"]}
    pull.update({name: {"pageInfo": last, "nodes": nodes[name]} for name in connections})
    return {"data": {"repository": {"pullRequest": pull}}}


def _api(args: list[str], state: dict) -> tuple[int, str, str]:
    pos, opts = _opts(args, {"-X", "-f", "-F", "--jq"})
    _allow_only(opts, {"-X", "-f", "-F", "--jq", "--paginate"})
    if len(pos) != 1:
        raise _Unrouted(f"api with positionals {pos}")
    endpoint = pos[0]
    if endpoint == "graphql":
        return _graphql(opts, state)
    method = _one(opts, "-X")
    fields = opts.get("-f", []) + opts.get("-F", [])
    # gh sends -f fields as a request body (a POST) unless -X GET says otherwise.
    if fields and method != "GET":
        raise _Unrouted("api with fields but no -X GET would POST")
    if method not in (None, "GET"):
        raise _Unrouted(f"api -X {method}")
    path, _, query = endpoint.partition("?")
    params = dict(parse_qsl(query, keep_blank_values=True))
    for item in fields:
        key, sep, value = item.partition("=")
        if not sep:
            raise _Unrouted(f"api field {item!r}")
        params[key] = value
    prefix = f"repos/{state['repo']}/"
    if not path.startswith(prefix):
        raise _Unrouted(f"api path outside {state['repo']}")
    rel = path[len(prefix) :]
    page = int(params.pop("page", "1"))
    per_page = int(params.pop("per_page", "30"))
    key = rel + ("?" + "&".join(f"{k}={v}" for k, v in sorted(params.items())) if params else "")
    if key not in state["api"]:
        raise _Unrouted(f"api {key!r}")
    value = state["api"][key]
    if isinstance(value, list) and "--paginate" not in opts:
        value = value[(page - 1) * per_page : page * per_page]
    return _answer(value, _one(opts, "--jq"))


def _route(argv: list[str], state: dict) -> tuple[int, str, str]:
    if len(argv) >= 2 and argv[:2] == ["pr", "view"]:
        return _pr_view(argv[2:], state)
    if len(argv) >= 2 and argv[:2] == ["pr", "list"]:
        return _pr_list(argv[2:], state)
    if len(argv) >= 2 and argv[:2] == ["repo", "view"]:
        return _repo_view(argv[2:], state)
    if len(argv) >= 2 and argv[0] == "run":
        return _run_cmd(argv[1:], state)
    if argv and argv[0] == "api":
        return _api(argv[1:], state)
    raise _Unrouted("no route for this command")


def main(argv: list[str]) -> int:
    try:
        state = json.loads(Path(os.environ[STATE_ENV]).read_text(encoding="utf-8"))
        rc, out, err = _route(argv, state)
        routed = True
    except Exception as exc:  # noqa: BLE001 - a crash in the fake is as loud as a miss.
        why = str(exc) if isinstance(exc, _Unrouted) else f"the fake raised {exc!r}"
        rc, out, err = 97, "", f"{UNROUTED}: {why}: {json.dumps(argv)}\n"
        routed = False
    with open(os.environ[LOG_ENV], "a", encoding="utf-8") as log:
        log.write(json.dumps({"argv": argv, "routed": routed, "rc": rc}) + "\n")
    sys.stdout.write(out)
    sys.stderr.write(err)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
