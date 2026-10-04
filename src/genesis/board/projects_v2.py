"""GitHub Projects v2 adapter — the board's only path to GitHub's GraphQL API.

Query shapes adapted from precursor-kanban's ``client.py`` (MIT, (c) 2026
Precursor contributors): project lookup by owner + number, paged ``items`` with
the Status field value, and ``updateProjectV2ItemFieldValue`` with a single-select
option id. Extended here with what that client does not cover (project, field,
view and workflow setup; item add; the per-value ``updatedAt`` the reconciler
keys on).

Behaviour that is MEASURED, not assumed (P1 probe on a private user project,
2026-10-03; Genesis memory 339996e5):

* No API creates, enables or reads the configuration of a built-in project
  workflow — only ``deleteProjectV2Workflow`` exists. A NEW project ships six
  defaults, including "Pull request linked to issue", which sets Status to
  In Progress by itself; setup deletes it.
* ``updateProjectV2Field`` OVERWRITES single-select options: an option re-sent
  WITH its ``id`` keeps every item's value, one without an id is new, and one
  left out is deleted (its item values cleared). :func:`merge_options` builds the
  list so existing options always keep their ids.
* Issue timelines on this account carry NO ``ProjectV2ItemStatusChangedEvent``,
  for UI drags and API writes alike; a change is detected from the Status
  value's ``updatedAt`` instead (:func:`list_items` returns it).
* Project reads lag writes by seconds; nothing here re-reads its own write and
  treats a stale answer as truth.

Transport: ``gh api graphql --input -`` with the JSON request on stdin, so a
query or a variable never reaches argv (no length limit, nothing for a shell to
interpret). Every GraphQL ``errors`` array raises :class:`ProjectsError` — an
error is never read back as empty data. The runner is injectable so tests never
touch the network.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: (returncode, stdout, stderr) for one ``gh api graphql`` call with *payload*
#: on stdin.
Runner = Callable[[str], Awaitable[tuple[int, str, str]]]

#: A gh call normally returns in well under 5 s. Without a bound, one hung
#: network call would wedge the caller forever — and the reconciler runs as a
#: max_instances=1 job, so a wedge stops the board, silently. 60 s matches the
#: contributor drain's existing gh bound (contributor_issue_watcher._GH_TIMEOUT).
GH_TIMEOUT_S = 60

STATUS_FIELD = "Status"
#: The board's columns, in order (spec §3.2).
STATUS_OPTIONS = ("Proposed", "Ready", "In Progress", "In Review", "Done")
GENESIS_FIELD = "Genesis"
#: Genesis's own per-card status (spec §3.1). Values carrying a free-text tail
#: ("Blocked: <reason>", "Waiting: until <t>") keep the tail in GENESIS_NOTE_FIELD.
GENESIS_OPTIONS = (
    "Building",
    "Reviewing",
    "Needs you: Telegram",
    "Needs you: join in tmux",
    "Blocked",
    "Stopped/Stalled",
    "Waiting: usage limit",
    "Merged, verifying",
    "Verified",
    "External: promote to work it",
)
GENESIS_NOTE_FIELD = "Genesis note"
#: Default-workflow names setup removes, and the ones it requires to exist.
WORKFLOWS_TO_DELETE = ("Pull request linked to issue", "Item added to project")
WORKFLOWS_REQUIRED = ("Pull request merged", "Item closed")

_COLORS = ("GRAY", "BLUE", "GREEN", "YELLOW", "ORANGE", "RED", "PINK", "PURPLE")


class ProjectsError(RuntimeError):
    """A GraphQL or transport failure. Never swallowed into an empty answer."""


async def _default_runner(payload: str) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "gh",
        "api",
        "graphql",
        "--input",
        "-",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(payload.encode()), timeout=GH_TIMEOUT_S)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"gh api graphql timed out after {GH_TIMEOUT_S}s"
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def graphql(
    query: str, variables: dict | None = None, *, runner: Runner | None = None
) -> dict:
    """Run one query/mutation; return ``data``. Raises :class:`ProjectsError` on
    a non-zero exit, unparseable output, or any ``errors`` entry."""
    payload = json.dumps({"query": query, "variables": variables or {}})
    rc, out, err = await (runner or _default_runner)(payload)
    try:
        doc = json.loads(out) if out.strip() else {}
    except ValueError as exc:
        raise ProjectsError(f"unparseable gh output (rc={rc}): {err.strip()[:300]}") from exc
    if doc.get("errors"):
        messages = "; ".join(str(e.get("message", e)) for e in doc["errors"])
        raise ProjectsError(f"GraphQL error: {messages}")
    if rc != 0 or "data" not in doc:
        raise ProjectsError(f"gh api graphql failed (rc={rc}): {err.strip()[:300]}")
    return doc["data"]


# ─── lookups ────────────────────────────────────────────────────────────────


@dataclass
class Field:
    id: str
    name: str
    kind: str  # "single_select" | "text" (a user TEXT field) | "other" (built-ins, dates, …)
    options: dict[str, str] = field(default_factory=dict)  # name -> option id
    raw_options: list[dict] = field(default_factory=list)


@dataclass
class Project:
    id: str
    number: int
    title: str
    public: bool
    fields: dict[str, Field]


_FIELDS_FRAGMENT = """
fields(first: 50) { totalCount nodes {
  __typename
  ... on ProjectV2FieldCommon { id name dataType }
  ... on ProjectV2SingleSelectField { options { id name color description } }
} }
"""


def _parse_project(node: dict) -> Project:
    fields: dict[str, Field] = {}
    fnodes = node["fields"]["nodes"]
    if node["fields"]["totalCount"] > len(fnodes):
        raise ProjectsError("project has more than 50 fields; refusing a partial field map")
    for f in fnodes:
        if not f or "name" not in f:
            continue
        if f["__typename"] == "ProjectV2SingleSelectField":
            opts = f.get("options") or []
            fields[f["name"]] = Field(
                f["id"], f["name"], "single_select", {o["name"]: o["id"] for o in opts}, opts
            )
        elif f.get("dataType") == "TEXT":
            # Built-ins (Title, Assignees, Labels, ...) are ProjectV2Field too;
            # only dataType tells a writable TEXT field apart from them.
            fields[f["name"]] = Field(f["id"], f["name"], "text")
        else:
            fields[f["name"]] = Field(f["id"], f["name"], "other")
    return Project(node["id"], node["number"], node["title"], node["public"], fields)


async def viewer(*, runner: Runner | None = None) -> dict:
    """``{"login", "id"}`` of the authenticated account."""
    data = await graphql("query { viewer { login id } }", runner=runner)
    return data["viewer"]


async def get_project(owner: str, number: int, *, runner: Runner | None = None) -> Project:
    data = await graphql(
        "query($o: String!, $n: Int!) { user(login: $o) { projectV2(number: $n) { id number title public "
        + _FIELDS_FRAGMENT
        + "} } }",
        {"o": owner, "n": number},
        runner=runner,
    )
    node = (data.get("user") or {}).get("projectV2")
    if node is None:
        raise ProjectsError(f"no project #{number} for user {owner}")
    return _parse_project(node)


async def find_projects_by_title(
    owner: str, title: str, *, runner: Runner | None = None
) -> list[dict]:
    """Every project of *owner* whose title equals *title* — paginated to the
    end, so "none found" is a complete answer, never a truncated one."""
    found, cursor = [], None
    while True:
        data = await graphql(
            "query($o: String!, $c: String) { user(login: $o) { projectsV2(first: 100, after: $c) {"
            " nodes { id number title closed } pageInfo { hasNextPage endCursor } } } }",
            {"o": owner, "c": cursor},
            runner=runner,
        )
        conn = data["user"]["projectsV2"]
        found += [p for p in conn["nodes"] if p and p["title"] == title and not p["closed"]]
        if not conn["pageInfo"]["hasNextPage"]:
            return found
        cursor = conn["pageInfo"]["endCursor"]


async def repository_id(owner: str, name: str, *, runner: Runner | None = None) -> str:
    data = await graphql(
        "query($o: String!, $n: String!) { repository(owner: $o, name: $n) { id } }",
        {"o": owner, "n": name},
        runner=runner,
    )
    if not data.get("repository"):
        raise ProjectsError(f"no repository {owner}/{name}")
    return data["repository"]["id"]


async def issue_node_id(owner: str, name: str, number: int, *, runner: Runner | None = None) -> str:
    data = await graphql(
        "query($o: String!, $n: String!, $i: Int!) { repository(owner: $o, name: $n) {"
        " issueOrPullRequest(number: $i) { ... on Issue { id } ... on PullRequest { id } } } }",
        {"o": owner, "n": name, "i": number},
        runner=runner,
    )
    node = (data.get("repository") or {}).get("issueOrPullRequest")
    if not node:
        raise ProjectsError(f"no issue or PR #{number} in {owner}/{name}")
    return node["id"]


# ─── setup mutations ────────────────────────────────────────────────────────


async def create_project(
    owner_id: str, title: str, repo_id: str, *, runner: Runner | None = None
) -> dict:
    """Create a user project linked to *repo_id*. GitHub creates it PRIVATE."""
    data = await graphql(
        "mutation($o: ID!, $t: String!, $r: ID!) { createProjectV2(input: {ownerId: $o, title: $t,"
        " repositoryId: $r}) { projectV2 { id number public } } }",
        {"o": owner_id, "t": title, "r": repo_id},
        runner=runner,
    )
    return data["createProjectV2"]["projectV2"]


def merge_options(
    existing: list[dict], wanted: tuple[str, ...], *, keep_unlisted: bool
) -> list[dict]:
    """The ``singleSelectOptions`` list for ``updateProjectV2Field`` that yields
    *wanted* in order WITHOUT clearing any item's value: every wanted option that
    already exists is re-sent with its ``id`` (identity preserved), new ones are
    sent without one. With ``keep_unlisted`` (the default for a live board),
    options outside *wanted* are kept, appended, so no card loses its value."""
    by_name = {o["name"]: o for o in existing}
    out = []
    for i, name in enumerate(wanted):
        cur = by_name.get(name)
        if cur is not None:
            out.append(
                {
                    "id": cur["id"],
                    "name": name,
                    "color": cur.get("color") or "GRAY",
                    "description": cur.get("description") or "",
                }
            )
        else:
            out.append({"name": name, "color": _COLORS[i % len(_COLORS)], "description": ""})
    if keep_unlisted:
        for o in existing:
            if o["name"] not in wanted:
                out.append(
                    {
                        "id": o["id"],
                        "name": o["name"],
                        "color": o.get("color") or "GRAY",
                        "description": o.get("description") or "",
                    }
                )
    return out


def _options_literal(options: list[dict]) -> str:
    """GraphQL input literal for option objects. ``color`` is an enum (bare);
    every string goes through json.dumps, so no value can break out of its slot."""
    parts = []
    for o in options:
        color = o["color"]
        if color not in _COLORS:
            raise ProjectsError(f"invalid option color {color!r}")
        fields = [
            f"name: {json.dumps(o['name'])}",
            f"color: {color}",
            f"description: {json.dumps(o.get('description') or '')}",
        ]
        if o.get("id"):
            fields.insert(0, f"id: {json.dumps(o['id'])}")
        parts.append("{" + ", ".join(fields) + "}")
    return "[" + ", ".join(parts) + "]"


async def update_single_select_options(
    field_id: str, options: list[dict], *, runner: Runner | None = None
) -> None:
    await graphql(
        "mutation($f: ID!) { updateProjectV2Field(input: {fieldId: $f, singleSelectOptions: "
        + _options_literal(options)
        + "}) { projectV2Field { ... on ProjectV2SingleSelectField { id } } } }",
        {"f": field_id},
        runner=runner,
    )


async def create_single_select_field(
    project_id: str, name: str, options: tuple[str, ...], *, runner: Runner | None = None
) -> None:
    opts = merge_options([], options, keep_unlisted=False)
    await graphql(
        "mutation($p: ID!, $n: String!) { createProjectV2Field(input: {projectId: $p, dataType: SINGLE_SELECT,"
        " name: $n, singleSelectOptions: " + _options_literal(opts) + "}) { projectV2Field { ... on"
        " ProjectV2SingleSelectField { id } } } }",
        {"p": project_id, "n": name},
        runner=runner,
    )


async def create_text_field(project_id: str, name: str, *, runner: Runner | None = None) -> None:
    await graphql(
        "mutation($p: ID!, $n: String!) { createProjectV2Field(input: {projectId: $p, dataType: TEXT,"
        " name: $n}) { projectV2Field { ... on ProjectV2Field { id } } } }",
        {"p": project_id, "n": name},
        runner=runner,
    )


async def list_workflows(project_id: str, *, runner: Runner | None = None) -> list[dict]:
    data = await graphql(
        "query($p: ID!) { node(id: $p) { ... on ProjectV2 { workflows(first: 50) { totalCount"
        " nodes { id name enabled } } } } }",
        {"p": project_id},
        runner=runner,
    )
    conn = data["node"]["workflows"]
    if conn["totalCount"] > len(conn["nodes"]):
        raise ProjectsError("more than 50 workflows; refusing a partial list")
    return conn["nodes"]


async def delete_workflow(workflow_id: str, *, runner: Runner | None = None) -> None:
    await graphql(
        "mutation($w: ID!) { deleteProjectV2Workflow(input: {workflowId: $w}) { deletedWorkflowId } }",
        {"w": workflow_id},
        runner=runner,
    )


async def list_views(project_id: str, *, runner: Runner | None = None) -> list[dict]:
    data = await graphql(
        "query($p: ID!) { node(id: $p) { ... on ProjectV2 { views(first: 50) { totalCount"
        " nodes { id name filter layout } } } } }",
        {"p": project_id},
        runner=runner,
    )
    conn = data["node"]["views"]
    if conn["totalCount"] > len(conn["nodes"]):
        raise ProjectsError("more than 50 views; refusing a partial list")
    return conn["nodes"]


async def create_view(project_id: str, name: str, *, runner: Runner | None = None) -> str:
    data = await graphql(
        "mutation($p: ID!, $n: String!) { createProjectV2View(input: {projectId: $p, name: $n,"
        " layout: BOARD_LAYOUT}) { projectV2View { id } } }",
        {"p": project_id, "n": name},
        runner=runner,
    )
    return data["createProjectV2View"]["projectV2View"]["id"]


async def set_view_filter(view_id: str, filter_: str, *, runner: Runner | None = None) -> None:
    await graphql(
        "mutation($v: ID!, $f: String!) { updateProjectV2View(input: {viewId: $v, filter: $f})"
        " { projectV2View { id } } }",
        {"v": view_id, "f": filter_},
        runner=runner,
    )


# ─── item operations (the reconciler's surface) ─────────────────────────────
# issue_node_id / add_item / item_status / set_single_select place a promoted
# issue on the board (the drain, autonomy.contributor_issue_watcher).
# GROUNDWORK(board-reconciler): set_text / list_items are the board
# reconciler's read and Genesis-field surface (the next board PR: it adds every
# open repo issue, makes the one bookkeeping move, and projects the Genesis
# status). Verified live against a private sandbox project.


async def add_item(project_id: str, content_id: str, *, runner: Runner | None = None) -> str:
    """Add an issue/PR; re-adding returns the SAME item (MEASURED), so this is
    idempotent and safe to retry."""
    data = await graphql(
        "mutation($p: ID!, $c: ID!) { addProjectV2ItemById(input: {projectId: $p, contentId: $c})"
        " { item { id } } }",
        {"p": project_id, "c": content_id},
        runner=runner,
    )
    return data["addProjectV2ItemById"]["item"]["id"]


async def item_status(item_id: str, *, runner: Runner | None = None) -> str | None:
    """The item's current Status option name, or None when it has none. A
    just-added item may not be readable yet (reads lag writes, MEASURED), which
    also reads as None: for a new item that is the truth."""
    data = await graphql(
        "query($i: ID!) { node(id: $i) { ... on ProjectV2Item { status: fieldValueByName("
        'name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name } } } } }',
        {"i": item_id},
        runner=runner,
    )
    node = data.get("node") or {}
    return (node.get("status") or {}).get("name")


async def set_single_select(
    project_id: str, item_id: str, field_id: str, option_id: str, *, runner: Runner | None = None
) -> None:
    await graphql(
        "mutation($p: ID!, $i: ID!, $f: ID!, $o: String!) { updateProjectV2ItemFieldValue(input:"
        " {projectId: $p, itemId: $i, fieldId: $f, value: {singleSelectOptionId: $o}}) { projectV2Item { id } } }",
        {"p": project_id, "i": item_id, "f": field_id, "o": option_id},
        runner=runner,
    )


async def set_text(
    project_id: str, item_id: str, field_id: str, text: str, *, runner: Runner | None = None
) -> None:
    await graphql(
        "mutation($p: ID!, $i: ID!, $f: ID!, $t: String!) { updateProjectV2ItemFieldValue(input:"
        " {projectId: $p, itemId: $i, fieldId: $f, value: {text: $t}}) { projectV2Item { id } } }",
        {"p": project_id, "i": item_id, "f": field_id, "t": text},
        runner=runner,
    )


_ITEMS_QUERY = """
query($p: ID!, $c: String) { node(id: $p) { ... on ProjectV2 { items(first: 100, after: $c) {
  totalCount
  pageInfo { hasNextPage endCursor }
  nodes {
    id updatedAt isArchived
    status: fieldValueByName(name: "Status") { ... on ProjectV2ItemFieldSingleSelectValue { name optionId updatedAt } }
    genesis: fieldValueByName(name: "Genesis") { ... on ProjectV2ItemFieldSingleSelectValue { name optionId } }
    note: fieldValueByName(name: "Genesis note") { ... on ProjectV2ItemFieldTextValue { text } }
    content {
      __typename
      ... on Issue { id number state url author { login } repository { nameWithOwner }
                     blockedBy(first: 20) { totalCount nodes { number state repository { nameWithOwner } } } }
      ... on PullRequest { id number state url author { login } repository { nameWithOwner } }
    }
  }
} } } }
"""


async def list_items(project_id: str, *, runner: Runner | None = None) -> dict:
    """Every item, paginated to the end. Returns ``{"items", "total"}`` and
    raises if the pages it read do not add up to the reported total — a short
    read is never presented as the whole board."""
    items, cursor, total = [], None, None
    while True:
        data = await graphql(_ITEMS_QUERY, {"p": project_id, "c": cursor}, runner=runner)
        conn = data["node"]["items"]
        total = conn["totalCount"]
        items += [n for n in conn["nodes"] if n]
        if not conn["pageInfo"]["hasNextPage"]:
            break
        cursor = conn["pageInfo"]["endCursor"]
    if len(items) != total:
        raise ProjectsError(f"read {len(items)} items but the project reports {total}")
    return {"items": items, "total": total}
