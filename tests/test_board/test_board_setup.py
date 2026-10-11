"""scripts/board_setup.py: a dry run reads and reports but NEVER mutates; a
run works only on the project recorded in the board overlay (or one it creates
and records), never one found by title; it never deletes a Status option; it
links the project to the tracker; and its overlay write keeps the legacy
overlay's keys. (The apply path was verified live against a private sandbox
project; see the PR body.)"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from genesis.board import config as board_config
from genesis.board import projects_v2 as pv
from tests.conftest import private_module

setup = private_module(
    "board_setup_under_test", Path(__file__).resolve().parents[2] / "scripts" / "board_setup.py"
)

MUTATIONS = (
    "create_project",
    "update_single_select_options",
    "create_single_select_field",
    "create_text_field",
    "delete_workflow",
    "create_view",
    "set_view_filter",
    "set_view_layout",
    "link_repository",
)


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """Base config + both overlay locations in tmp: ``(legacy, user)`` paths."""
    repo_dir = tmp_path / "repo"
    user_dir = tmp_path / "user_config"
    (repo_dir / "config").mkdir(parents=True)
    user_dir.mkdir()
    monkeypatch.setattr(board_config, "repo_root", lambda: repo_dir)
    monkeypatch.setattr("genesis._config_overlay._user_config_dir", lambda: user_dir)
    monkeypatch.delenv(board_config._DISABLE_ENV, raising=False)
    return repo_dir / "config" / "board.local.yaml", user_dir / "board.local.yaml"


@pytest.fixture
def recorded(dirs):
    """The board overlay records project #4 of ``owner``."""
    _legacy, user = dirs
    user.write_text("project_owner: owner\nproject_number: 4\n")
    return 4


@pytest.fixture
def fake_pv(monkeypatch, dirs):
    mutated: list[str] = []

    def forbid(name):
        async def _f(*_a, **_k):
            mutated.append(name)

        return _f

    for name in MUTATIONS:
        monkeypatch.setattr(pv, name, forbid(name))

    async def viewer(**_k):
        return {"login": "owner", "id": "U1"}

    async def project(owner, number, **_k):
        return pv.Project(
            "P1",
            number,
            "Genesis Work Board",
            False,
            {
                "Status": pv.Field(
                    "F1",
                    "Status",
                    "single_select",
                    {"Todo": "t"},
                    [{"id": "t", "name": "Todo", "color": "GRAY", "description": ""}],
                ),
            },
        )

    async def items(_pid, **_k):
        raise AssertionError("setup must not decide anything from a snapshot of the cards")

    async def workflows(_pid, **_k):
        return [{"id": "W1", "name": "Pull request linked to issue", "enabled": True}]

    async def views(_pid, **_k):
        return []

    async def repos(_pid, **_k):
        return ["Owner/Repo"]  # linked (GitHub owner/name is case-insensitive)

    async def repo_id(owner, name, **_k):
        return "R1"

    async def no_title_match(owner, title, **_k):
        return []

    monkeypatch.setattr(pv, "viewer", viewer)
    monkeypatch.setattr(pv, "get_project", project)
    monkeypatch.setattr(pv, "list_items", items)
    monkeypatch.setattr(pv, "list_workflows", workflows)
    monkeypatch.setattr(pv, "list_views", views)
    monkeypatch.setattr(pv, "project_repositories", repos)
    monkeypatch.setattr(pv, "repository_id", repo_id)
    monkeypatch.setattr(pv, "find_projects_by_title", no_title_match)
    monkeypatch.setattr(setup, "_repo_slug", lambda: "owner/repo")
    return mutated


def _project_with(fields):
    async def project(owner, number, **_k):
        base = {
            "Status": pv.Field(
                "F1",
                "Status",
                "single_select",
                {n: n for n in pv.STATUS_OPTIONS},
                [
                    {"id": n, "name": n, "color": "GRAY", "description": ""}
                    for n in pv.STATUS_OPTIONS
                ],
            ),
            "Genesis": pv.Field(
                "F2",
                "Genesis",
                "single_select",
                {n: n for n in pv.GENESIS_OPTIONS},
                [
                    {"id": n, "name": n, "color": "GRAY", "description": ""}
                    for n in pv.GENESIS_OPTIONS
                ],
            ),
            "Genesis note": pv.Field("F3", "Genesis note", "text"),
        }
        base.update(fields)
        return pv.Project("P1", number, "Genesis Work Board", False, base)

    return project


async def test_dry_run_reports_every_change_and_mutates_nothing(fake_pv, recorded, capsys):
    code = await setup.run("Genesis Work Board", apply=False, write_config=True)
    out = capsys.readouterr().out
    assert fake_pv == [], "a dry run must not call a single mutation"
    assert "project #4 'Genesis Work Board' (recorded)" in out
    assert (
        "would set Status options" in out
        and "would delete workflow 'Pull request linked to issue'" in out
    )
    assert "would create view 'Active'" in out
    assert "PROBLEM: workflow 'Pull request merged' is missing" in out
    assert code == 1, "a missing required workflow is a non-zero exit"


async def test_a_public_project_is_refused_without_allow_public(
    fake_pv, recorded, monkeypatch, capsys
):
    async def public_project(owner, number, **_k):
        return pv.Project("P1", number, "Genesis Work Board", True, {})

    monkeypatch.setattr(pv, "get_project", public_project)
    assert await setup.run("Genesis Work Board", apply=True, write_config=False) == 2
    assert "REFUSING: the project is PUBLIC" in capsys.readouterr().out
    assert fake_pv == []


async def test_a_wrongly_typed_genesis_field_is_a_problem_not_a_mutation(
    fake_pv, recorded, monkeypatch, capsys
):
    async def project_with_text_genesis(owner, number, **_k):
        return pv.Project(
            "P1",
            number,
            "Genesis Work Board",
            False,
            {
                "Status": pv.Field("F1", "Status", "single_select", {}, []),
                "Genesis": pv.Field("F2", "Genesis", "text"),
            },
        )

    monkeypatch.setattr(pv, "get_project", project_with_text_genesis)
    updated = []

    async def capture(field_id, options, **_k):
        updated.append(field_id)

    monkeypatch.setattr(pv, "update_single_select_options", capture)
    assert await setup.run(setup.DEFAULT_TITLE, apply=True, write_config=False) == 1
    assert "F2" not in updated
    assert "not single-select" in capsys.readouterr().out


def test_the_board_links_the_configured_tracker_not_the_checkout(monkeypatch):
    monkeypatch.setattr(board_config, "tracker_repo", lambda: ("upstream", "Repo"))
    assert setup._repo_slug() == "upstream/Repo"
    monkeypatch.setattr(board_config, "tracker_repo", lambda: None)
    with pytest.raises(SystemExit):
        setup._repo_slug()


async def test_a_wrongly_typed_genesis_note_is_a_problem(fake_pv, recorded, monkeypatch, capsys):
    monkeypatch.setattr(
        pv, "get_project", _project_with({"Genesis note": pv.Field("F3", "Genesis note", "other")})
    )
    assert await setup.run(setup.DEFAULT_TITLE, apply=True, write_config=False) == 1
    assert "not a text field" in capsys.readouterr().out
    assert "create_text_field" not in fake_pv


async def test_an_existing_view_with_the_wrong_layout_is_fixed(
    fake_pv, recorded, monkeypatch, capsys
):
    async def views(_pid, **_k):
        return [
            {"id": "V1", "name": "Active", "filter": "-status:Proposed", "layout": "TABLE_LAYOUT"},
            {"id": "V2", "name": "Backlog", "filter": "status:Proposed", "layout": "BOARD_LAYOUT"},
        ]

    monkeypatch.setattr(pv, "get_project", _project_with({}))
    monkeypatch.setattr(pv, "list_views", views)
    await setup.run(setup.DEFAULT_TITLE, apply=False, write_config=False)
    out = capsys.readouterr().out
    assert "would set view 'Active' layout 'TABLE_LAYOUT' -> 'BOARD_LAYOUT'" in out
    assert "view 'Backlog' OK" in out
    assert fake_pv == []  # a dry run mutates nothing
    await setup.run(setup.DEFAULT_TITLE, apply=True, write_config=False)
    assert fake_pv.count("set_view_layout") == 1


# ─── round 3: the narrowed setup ────────────────────────────────────────────


async def test_an_unlisted_status_option_is_never_deleted(fake_pv, recorded, monkeypatch):
    """Whatever the cards hold now: a card can take the option between any
    read and the write, so the option is kept, never re-checked and dropped."""
    sent = {}

    async def capture(field_id, options, **_k):
        sent["options"] = options

    monkeypatch.setattr(pv, "update_single_select_options", capture)
    await setup.run(setup.DEFAULT_TITLE, apply=True, write_config=False)
    by_name = {o["name"]: o for o in sent["options"]}
    assert [o["name"] for o in sent["options"]][: len(pv.STATUS_OPTIONS)] == list(pv.STATUS_OPTIONS)
    assert by_name["Todo"]["id"] == "t", "kept, WITH its id, so no card loses its value"


@pytest.mark.parametrize("count", [1, 2])
async def test_a_same_titled_project_that_is_not_recorded_is_refused(
    fake_pv, monkeypatch, capsys, count
):
    async def found(owner, title, **_k):
        return [{"id": f"P{i}", "number": i, "title": title, "closed": False} for i in range(count)]

    async def must_not_read(*_a, **_k):
        raise AssertionError("an unrecorded project was read as if it were the board")

    monkeypatch.setattr(pv, "find_projects_by_title", found)
    monkeypatch.setattr(pv, "get_project", must_not_read)
    assert await setup.run(setup.DEFAULT_TITLE, apply=True, write_config=True) == 2
    out = capsys.readouterr().out
    assert "never adopts a project by its title" in out
    assert fake_pv == []


async def test_a_new_project_is_recorded_then_stops_for_a_rerun(fake_pv, dirs, monkeypatch, capsys):
    """Project reads lag writes: nothing created this run is checked by it. The
    number it records is what identifies the project to every later run."""
    _legacy, user = dirs

    async def create(owner_id, title, repo, **_k):
        fake_pv.append("create_project")
        return {"id": "P9", "number": 9, "public": False}

    async def must_not_read(*_a, **_k):
        raise AssertionError("a freshly created project was read in the same run")

    monkeypatch.setattr(pv, "create_project", create)
    monkeypatch.setattr(pv, "get_project", must_not_read)
    assert await setup.run(setup.DEFAULT_TITLE, apply=True, write_config=True) == 3
    assert fake_pv == ["create_project"]
    assert "re-run" in capsys.readouterr().out
    assert yaml.safe_load(user.read_text()) == {"project_owner": "owner", "project_number": 9}
    assert board_config.project_ref() == ("owner", 9)


async def test_a_create_without_write_config_is_refused(fake_pv, dirs, capsys):
    assert await setup.run(setup.DEFAULT_TITLE, apply=True, write_config=False) == 2
    assert fake_pv == []
    assert "--write-config" in capsys.readouterr().out


async def test_the_legacy_overlay_is_carried_into_the_user_overlay(
    fake_pv, dirs, monkeypatch, capsys
):
    """The runtime reads the repo-local overlay until a user overlay exists;
    writing the user overlay must not make its settings vanish."""
    legacy, user = dirs
    legacy.write_text("enabled: true\nmode: live\n")
    assert board_config.effective_mode() == "live"

    async def create(owner_id, title, repo, **_k):
        return {"id": "P9", "number": 9, "public": False}

    monkeypatch.setattr(pv, "create_project", create)
    assert await setup.run(setup.DEFAULT_TITLE, apply=True, write_config=True) == 3
    assert yaml.safe_load(user.read_text()) == {
        "enabled": True,
        "mode": "live",
        "project_owner": "owner",
        "project_number": 9,
    }
    assert board_config.effective_mode() == "live", "the legacy setting still holds"
    assert board_config.project_ref() == ("owner", 9)
    assert "carried every key of the legacy overlay" in capsys.readouterr().out


async def test_an_unreadable_overlay_refuses_before_creating_anything(fake_pv, dirs, capsys):
    _legacy, user = dirs
    user.write_text("- not\n- a mapping\n")
    assert await setup.run(setup.DEFAULT_TITLE, apply=True, write_config=True) == 2
    assert fake_pv == [], "no project is created that could not be recorded"


async def test_an_unlinked_project_is_linked_dry_run_first(fake_pv, recorded, monkeypatch, capsys):
    async def other_repos(_pid, **_k):
        return ["someone/else"]

    monkeypatch.setattr(pv, "project_repositories", other_repos)
    monkeypatch.setattr(pv, "get_project", _project_with({}))
    await setup.run(setup.DEFAULT_TITLE, apply=False, write_config=False)
    assert "would link project #4 to owner/repo" in capsys.readouterr().out
    assert fake_pv == []
    linked = []

    async def link(project_id, repo_id, **_k):
        linked.append((project_id, repo_id))

    monkeypatch.setattr(pv, "link_repository", link)
    await setup.run(setup.DEFAULT_TITLE, apply=True, write_config=False)
    assert linked == [("P1", "R1")]


async def test_a_linked_project_is_left_alone(fake_pv, recorded, monkeypatch, capsys):
    monkeypatch.setattr(pv, "get_project", _project_with({}))
    await setup.run(setup.DEFAULT_TITLE, apply=True, write_config=False)
    assert "project linked to owner/repo OK" in capsys.readouterr().out
    assert "link_repository" not in fake_pv


@pytest.mark.parametrize("case", ["other-owner", "unreadable", "closed"])
async def test_a_recorded_project_that_is_not_usable_is_refused(
    fake_pv, dirs, monkeypatch, capsys, case
):
    _legacy, user = dirs
    owner = "someone" if case == "other-owner" else "owner"
    user.write_text(f"project_owner: {owner}\nproject_number: 4\n")

    async def unreadable(owner, number, **_k):
        raise pv.ProjectsError("no project #4 for user owner")

    async def closed(owner, number, **_k):
        return pv.Project("P1", number, "Genesis Work Board", False, {}, closed=True)

    if case == "unreadable":
        monkeypatch.setattr(pv, "get_project", unreadable)
    elif case == "closed":
        monkeypatch.setattr(pv, "get_project", closed)
    assert await setup.run(setup.DEFAULT_TITLE, apply=True, write_config=False) == 2
    assert "REFUSING" in capsys.readouterr().out
    assert fake_pv == []


async def test_a_clean_recorded_project_reports_ok(fake_pv, recorded, monkeypatch, capsys):
    async def workflows(_pid, **_k):
        return [{"id": f"W{n}", "name": n, "enabled": True} for n in pv.WORKFLOWS_REQUIRED]

    async def views(_pid, **_k):
        return [
            {"id": "V1", "name": n, "filter": f, "layout": pv.BOARD_LAYOUT}
            for n, f in setup.VIEWS.items()
        ]

    monkeypatch.setattr(pv, "get_project", _project_with({}))
    monkeypatch.setattr(pv, "list_workflows", workflows)
    monkeypatch.setattr(pv, "list_views", views)
    assert await setup.run(setup.DEFAULT_TITLE, apply=False, write_config=True) == 0
    assert "config overlay OK" in capsys.readouterr().out
    assert fake_pv == []
