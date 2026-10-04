#!/usr/bin/env python3
"""Set up (or re-check) the work board on GitHub Projects v2 — idempotent.

DRY RUN BY DEFAULT: prints what it would do and changes nothing. ``--apply``
performs the GitHub changes; ``--write-config`` also records the project in the
user overlay (``~/.genesis/config/board.local.yaml``). Both are outward actions
the owner runs deliberately; nothing calls this script automatically.

What it ensures, in order (every step is a no-op when already true):

1. ONE open user project titled ``--title`` (default "Genesis Work Board"),
   linked to the configured public tracker (``github.user`` /
   ``github.public_repo`` — the repo promotions post to, never this checkout's
   own remote, which on a fork clone is the operator's fork). Zero -> create
   (GitHub creates it PRIVATE) and STOP: project reads lag writes by seconds
   (MEASURED), so a freshly created project is checked by a re-run, never by
   this one. More than one -> refuse, never guess which.
2. Status options = Proposed / Ready / In Progress / In Review / Done, with every
   existing option re-sent with its id (no card loses its value). An option
   outside that list is dropped only when NO item uses it.
3. A "Genesis" single-select field (Genesis's own per-card status) and a
   "Genesis note" text field.
4. Workflows: deletes "Pull request linked to issue" (it sets In Progress by
   itself, and only a human may start work) and "Item added to project" (the
   drain sets Proposed on a promoted issue, the reconciler on every other
   one). Requires "Pull request merged" and "Item closed" to exist and be
   enabled; GitHub offers NO API to create or enable a
   workflow, so a missing one is reported with the UI step, and the exit code
   is non-zero.
5. Views "Active" (``-status:Proposed``) and "Backlog" (``status:Proposed``).

MEASURED behaviour this relies on: Genesis memory 339996e5 and the
``genesis.board.projects_v2`` module docstring.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

DEFAULT_TITLE = "Genesis Work Board"
VIEWS = {"Active": "-status:Proposed", "Backlog": "status:Proposed"}


def _repo_slug() -> str:
    """The public tracker promotions post to (``board.config.tracker_repo``)."""
    from genesis.board import config as board_config

    tracker = board_config.tracker_repo()
    if tracker is None:
        raise SystemExit("no public tracker configured (github.user / github.public_repo)")
    return "/".join(tracker)


async def run(title: str, apply: bool, write_config: bool, allow_public: bool = False) -> int:
    from genesis.board import projects_v2 as pv

    say = print
    act = "" if apply else "[dry-run] would "
    problems = 0

    me = await pv.viewer()
    owner, owner_id = me["login"], me["id"]
    slug = _repo_slug()
    say(f"account: {owner}; repo: {slug}")

    found = await pv.find_projects_by_title(owner, title)
    if len(found) > 1:
        say(
            f"REFUSING: {len(found)} open projects titled {title!r} ({[p['number'] for p in found]}); close extras first"
        )
        return 2
    if not found:
        say(f"{act}create private project {title!r} linked to {slug}")
        if not apply:
            say("(nothing further can be checked until the project exists)")
            return 0
        repo_owner, repo_name = slug.split("/", 1)
        created = await pv.create_project(
            owner_id, title, await pv.repository_id(repo_owner, repo_name)
        )
        say(
            f"created project #{created['number']} (public={created['public']}). GitHub reads "
            "lag writes: wait a minute, then re-run this command to finish setup and record the config (an immediate re-run may not see the new project yet)."
        )
        return 3
    else:
        number = found[0]["number"]
        say(f"project #{number} {title!r} exists")

    proj = await pv.get_project(owner, number)
    if proj.public and not allow_public:
        say(
            "REFUSING: the project is PUBLIC, so Genesis's status text would be visible to anyone. "
            "Make it private in the project settings, or re-run with --allow-public to accept that."
        )
        return 2

    # 2. Status options — never clear a card's value.
    status = proj.fields.get(pv.STATUS_FIELD)
    if status is None or status.kind != "single_select":
        say("PROBLEM: no single-select Status field")
        problems += 1
    else:
        used = {
            (it.get("status") or {}).get("name") for it in (await pv.list_items(proj.id))["items"]
        }
        unlisted = [o["name"] for o in status.raw_options if o["name"] not in pv.STATUS_OPTIONS]
        keep_unlisted = any(name in used for name in unlisted)
        wanted = pv.merge_options(
            status.raw_options, pv.STATUS_OPTIONS, keep_unlisted=keep_unlisted
        )
        current = [o["name"] for o in status.raw_options]
        target = [o["name"] for o in wanted]
        if current != target:
            say(
                f"{act}set Status options {current} -> {target}"
                + (" (unlisted options kept: a card uses one)" if keep_unlisted else "")
            )
            if apply:
                await pv.update_single_select_options(status.id, wanted)
        else:
            say("Status options OK")

    # 3. Genesis fields.
    gen = proj.fields.get(pv.GENESIS_FIELD)
    if gen is not None and gen.kind != "single_select":
        say(
            f"PROBLEM: field {pv.GENESIS_FIELD!r} exists but is not single-select; rename or delete it"
        )
        problems += 1
    elif gen is None:
        say(f"{act}create single-select field {pv.GENESIS_FIELD!r}")
        if apply:
            await pv.create_single_select_field(proj.id, pv.GENESIS_FIELD, pv.GENESIS_OPTIONS)
    else:
        missing = [n for n in pv.GENESIS_OPTIONS if n not in gen.options]
        if missing:
            say(f"{act}add {pv.GENESIS_FIELD!r} options {missing}")
            if apply:
                await pv.update_single_select_options(
                    gen.id,
                    pv.merge_options(gen.raw_options, pv.GENESIS_OPTIONS, keep_unlisted=True),
                )
        else:
            say(f"{pv.GENESIS_FIELD!r} field OK")
    if pv.GENESIS_NOTE_FIELD not in proj.fields:
        say(f"{act}create text field {pv.GENESIS_NOTE_FIELD!r}")
        if apply:
            await pv.create_text_field(proj.id, pv.GENESIS_NOTE_FIELD)
    else:
        say(f"{pv.GENESIS_NOTE_FIELD!r} field OK")

    # 4. Workflows.
    workflows = await pv.list_workflows(proj.id)
    for wf in workflows:
        if wf["name"] in pv.WORKFLOWS_TO_DELETE:
            say(f"{act}delete workflow {wf['name']!r}")
            if apply:
                await pv.delete_workflow(wf["id"])
    by_name = {w["name"]: w for w in workflows}
    for name in pv.WORKFLOWS_REQUIRED:
        wf = by_name.get(name)
        if wf is None or not wf["enabled"]:
            problems += 1
            say(
                f"PROBLEM: workflow {name!r} is {'disabled' if wf else 'missing'} — GitHub has no API for "
                f"this; enable it in the project's Workflows settings"
            )
        else:
            say(f"workflow {name!r} OK")

    # 5. Views.
    views = {v["name"]: v for v in await pv.list_views(proj.id)}
    for name, filt in VIEWS.items():
        view = views.get(name)
        if view is None:
            say(f"{act}create view {name!r} with filter {filt!r}")
            if apply:
                await pv.set_view_filter(await pv.create_view(proj.id, name), filt)
        elif view.get("filter") != filt:
            say(f"{act}set view {name!r} filter {view.get('filter')!r} -> {filt!r}")
            if apply:
                await pv.set_view_filter(view["id"], filt)
        else:
            say(f"view {name!r} OK")

    # Config overlay.
    if write_config:
        import yaml

        from genesis._config_overlay import _user_config_dir

        path = _user_config_dir() / "board.local.yaml"
        data = {}
        if path.exists():
            loaded = yaml.safe_load(path.read_text()) or {}
            if not isinstance(loaded, dict):
                say(f"REFUSING to rewrite {path}: it is not a mapping")
                return 2
            data = loaded
        if data.get("project_owner") == owner and data.get("project_number") == number:
            say(f"config overlay OK ({path})")
        else:
            say(f"{act}record project_owner={owner} project_number={number} in {path}")
            if apply:
                data.update({"project_owner": owner, "project_number": number})
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(yaml.safe_dump(data, sort_keys=False))
    say(json.dumps({"project_number": number, "problems": problems, "applied": apply}))
    return 1 if problems else 0


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--title", default=DEFAULT_TITLE, help="project title (default %(default)r)")
    ap.add_argument(
        "--apply", action="store_true", help="perform the GitHub changes (default: dry run)"
    )
    ap.add_argument(
        "--write-config", action="store_true", help="record the project in the user overlay"
    )
    ap.add_argument(
        "--allow-public",
        action="store_true",
        help="accept an existing PUBLIC project (status text becomes public)",
    )
    args = ap.parse_args()
    raise SystemExit(asyncio.run(run(args.title, args.apply, args.write_config, args.allow_public)))


if __name__ == "__main__":
    main()
