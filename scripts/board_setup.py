#!/usr/bin/env python3
"""Set up (or re-check) the work board on GitHub Projects v2 — idempotent.

DRY RUN BY DEFAULT: prints what it would do and changes nothing. ``--apply``
performs the GitHub changes; ``--write-config`` lets a run that CREATES the
project record it in the board overlay. Both are outward actions the owner runs
deliberately; nothing calls this script automatically.

Which project: ONLY the one recorded in the board overlay
(``project_owner`` / ``project_number``, read exactly as the runtime reads it),
or one this script creates. It never adopts a project by its title — a
same-titled project nobody recorded may be anyone's, and setup deletes
workflows and rewrites fields — so an unrecorded same-titled project is a
refusal: record the one you mean, or rename it.

What it ensures, in order (every step is a no-op when already true):

1. The project. Recorded -> it must exist, be open, and belong to the
   authenticated account. Nothing recorded and no open project titled
   ``--title`` (default "Genesis Work Board") -> create it (GitHub creates it
   PRIVATE), record it (needs ``--write-config``) and STOP: project reads lag
   writes by seconds (MEASURED), so a freshly created project is checked by a
   re-run, never by this one.
2. The project is linked to the configured public tracker (``github.user`` /
   ``github.public_repo`` — the repo promotions post to, never this checkout's
   own remote, which on a fork clone is the operator's fork), read from
   ``ProjectV2.repositories``; linked with ``linkProjectV2ToRepository`` if not.
3. Status options = Proposed / Ready / In Progress / In Review / Done, with every
   existing option re-sent with its id. An option outside that list is ALWAYS
   kept: deleting one clears it from every card that holds it, and no read
   made before the write can prove no card holds it AT the write.
4. A "Genesis" single-select field (Genesis's own per-card status) and a
   "Genesis note" text field.
5. Workflows: deletes "Pull request linked to issue" (it sets In Progress by
   itself, and only a human may start work) and "Item added to project" (the
   board reconciler sets a new card's Status itself). Requires "Pull request
   merged" and "Item closed" to exist and be enabled; GitHub offers NO API to
   create or enable a workflow, so a missing one is reported with the UI step,
   and the exit code is non-zero.
6. Views "Active" (``-status:Proposed``) and "Backlog" (``status:Proposed``).

The overlay write loads the SAME overlay the runtime resolves
(``genesis._config_overlay``: the user dir first, then the legacy repo-local
``config/board.local.yaml``) and keeps every key in it, so recording the project
in the user overlay never hides a setting that lived in the legacy one.

MEASURED behaviour this relies on: the ``genesis.board.projects_v2`` module
docstring.
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


def _overlay_plan() -> tuple[Path, Path, dict | None]:
    """``(read_from, write_to, data)`` for recording the project.

    *read_from* is the overlay the runtime reads RIGHT NOW
    (``_resolve_overlay_path``: the user dir first, then the legacy repo-local
    file); *write_to* is the user overlay, which ``merge_local_overlay`` prefers
    from the moment it exists. Writing the user overlay without carrying the
    legacy file's keys would silently drop them (``mode``, ``enabled``, ...), so
    *data* is that file's whole mapping — ``{}`` when there is none, None when
    it cannot be read as a mapping (the caller refuses)."""
    import yaml

    from genesis._config_overlay import _resolve_overlay_path, _user_config_dir
    from genesis.board import config as board_config

    read_from = _resolve_overlay_path(board_config._base_path())
    write_to = _user_config_dir() / "board.local.yaml"
    if not read_from.exists():
        return read_from, write_to, {}
    try:
        loaded = yaml.safe_load(read_from.read_text())
    except (OSError, yaml.YAMLError):
        return read_from, write_to, None
    if loaded is None:
        return read_from, write_to, {}
    return read_from, write_to, loaded if isinstance(loaded, dict) else None


def _record_project(plan: tuple[Path, Path, dict], owner: str, number: int, say) -> None:
    import yaml

    from genesis.util.atomic import atomic_write_text

    read_from, write_to, data = plan
    data = {**data, "project_owner": owner, "project_number": number}
    atomic_write_text(write_to, yaml.safe_dump(data, sort_keys=False))
    if read_from != write_to:
        say(f"carried every key of the legacy overlay {read_from} into {write_to}")
    say(f"recorded project_owner={owner} project_number={number} in {write_to}")


async def _identify_project(pv, title, owner, owner_id, slug, apply, write_config, act, say):
    """The recorded project (``(number, None)``), or an exit code
    (``(None, code)``) when there is nothing to set up in this run: a refusal, a
    dry run of the create, or a create that a re-run must finish."""
    from genesis.board import config as board_config

    ref = board_config.project_ref()
    if ref is not None:
        recorded_owner, number = ref
        # GitHub logins are case-insensitive; a hand-written overlay may differ in case.
        if recorded_owner.lower() != owner.lower():
            say(
                f"REFUSING: the recorded project belongs to {recorded_owner!r}, but gh is "
                f"authenticated as {owner!r}"
            )
            return None, 2
        return number, None

    cfg = board_config.load_config()
    if cfg.get("project_owner") is not None or cfg.get("project_number") is not None:
        say("REFUSING: the board overlay's project_owner/project_number are malformed; fix them")
        return None, 2
    plan = _overlay_plan()
    found = await pv.find_projects_by_title(owner, title)
    if found:
        # Point at the file the runtime reads now: a hand-made user overlay
        # would hide a legacy repo-local one (and every key in it).
        where = plan[0] if plan[0].exists() else plan[1]
        say(
            f"REFUSING: open project(s) titled {title!r} exist ({[p['number'] for p in found]}) "
            f"but none is recorded in the board config, and setup never adopts a project by "
            f"its title. Record the one you mean (project_owner: {owner}, project_number: <n>) "
            f"in {where}, or rename it, then re-run."
        )
        return None, 2
    if plan[2] is None:
        say(f"REFUSING: the board overlay {plan[0]} is not a readable mapping; fix it first")
        return None, 2
    if not write_config:
        say(
            f"REFUSING to create project {title!r} without --write-config: a project this "
            "script creates is identified by the number it records, never by its title"
        )
        return None, 2
    say(f"{act}create private project {title!r} linked to {slug}, and record it in {plan[1]}")
    if not apply:
        say("(nothing further can be checked until the project exists)")
        return None, 0
    repo_owner, repo_name = slug.split("/", 1)
    created = await pv.create_project(
        owner_id, title, await pv.repository_id(repo_owner, repo_name)
    )
    _record_project(plan, owner, created["number"], say)
    say(
        f"created project #{created['number']} (public={created['public']}). GitHub reads "
        "lag writes: wait a minute, then re-run this command to finish setup (an immediate "
        "re-run may not see the new project yet)."
    )
    return None, 3


async def run(title: str, apply: bool, write_config: bool, allow_public: bool = False) -> int:
    from genesis.board import projects_v2 as pv

    say = print
    act = "" if apply else "[dry-run] would "
    problems = 0

    me = await pv.viewer()
    owner, owner_id = me["login"], me["id"]
    slug = _repo_slug()
    say(f"account: {owner}; repo: {slug}")

    # 1. The project: the recorded one, or one this run creates.
    number, code = await _identify_project(
        pv, title, owner, owner_id, slug, apply, write_config, act, say
    )
    if number is None:
        return code
    try:
        proj = await pv.get_project(owner, number)
    except pv.ProjectsError as exc:
        say(f"REFUSING: the recorded project #{number} cannot be read ({exc}); fix the record")
        return 2
    if proj.closed:
        say(f"REFUSING: the recorded project #{number} is closed; reopen it or fix the record")
        return 2
    say(f"project #{number} {proj.title!r} (recorded)")
    if proj.public and not allow_public:
        say(
            "REFUSING: the project is PUBLIC, so Genesis's status text would be visible to anyone. "
            "Make it private in the project settings, or re-run with --allow-public to accept that."
        )
        return 2

    # 2. Linked to the tracker promotions post to.
    linked = {r.lower() for r in await pv.project_repositories(proj.id)}
    if slug.lower() in linked:
        say(f"project linked to {slug} OK")
    else:
        say(f"{act}link project #{number} to {slug}")
        if apply:
            repo_owner, repo_name = slug.split("/", 1)
            await pv.link_repository(proj.id, await pv.repository_id(repo_owner, repo_name))

    # 3. Status options — never delete one: that clears it from every card.
    status = proj.fields.get(pv.STATUS_FIELD)
    if status is None or status.kind != "single_select":
        say("PROBLEM: no single-select Status field")
        problems += 1
    else:
        wanted = pv.merge_options(status.raw_options, pv.STATUS_OPTIONS, keep_unlisted=True)
        current = [o["name"] for o in status.raw_options]
        target = [o["name"] for o in wanted]
        if current != target:
            kept = [n for n in current if n not in pv.STATUS_OPTIONS]
            say(
                f"{act}set Status options {current} -> {target}"
                + (f" (options outside the board's columns kept: {kept})" if kept else "")
            )
            if apply:
                await pv.update_single_select_options(status.id, wanted)
        else:
            say("Status options OK")

    # 4. Genesis fields.
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
    note = proj.fields.get(pv.GENESIS_NOTE_FIELD)
    if note is not None and note.kind != "text":
        say(
            f"PROBLEM: field {pv.GENESIS_NOTE_FIELD!r} exists but is not a text field; rename or delete it"
        )
        problems += 1
    elif note is None:
        say(f"{act}create text field {pv.GENESIS_NOTE_FIELD!r}")
        if apply:
            await pv.create_text_field(proj.id, pv.GENESIS_NOTE_FIELD)
    else:
        say(f"{pv.GENESIS_NOTE_FIELD!r} field OK")

    # 5. Workflows.
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

    # 6. Views.
    views = {v["name"]: v for v in await pv.list_views(proj.id)}
    for name, filt in VIEWS.items():
        view = views.get(name)
        if view is None:
            say(f"{act}create view {name!r} with filter {filt!r}")
            if apply:
                await pv.set_view_filter(await pv.create_view(proj.id, name), filt)
        else:
            ok = True
            if view.get("layout") != pv.BOARD_LAYOUT:
                ok = False
                say(f"{act}set view {name!r} layout {view.get('layout')!r} -> {pv.BOARD_LAYOUT!r}")
                if apply:
                    await pv.set_view_layout(view["id"], pv.BOARD_LAYOUT)
            if view.get("filter") != filt:
                ok = False
                say(f"{act}set view {name!r} filter {view.get('filter')!r} -> {filt!r}")
                if apply:
                    await pv.set_view_filter(view["id"], filt)
            if ok:
                say(f"view {name!r} OK")

    if write_config:
        say("config overlay OK (this project is the one recorded there)")
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
        "--write-config",
        action="store_true",
        help="let a run that creates the project record it in the board overlay",
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
