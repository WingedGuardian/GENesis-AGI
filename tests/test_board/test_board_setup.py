"""scripts/board_setup.py: a dry run reads and reports but NEVER mutates, and
a run refuses to guess between two same-titled projects. (The apply path was
verified live against a private sandbox project; see the PR body.)"""

from __future__ import annotations

from pathlib import Path

import pytest

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
)


@pytest.fixture
def fake_pv(monkeypatch):
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
        return {"items": [], "total": 0}

    async def workflows(_pid, **_k):
        return [{"id": "W1", "name": "Pull request linked to issue", "enabled": True}]

    async def views(_pid, **_k):
        return []

    monkeypatch.setattr(pv, "viewer", viewer)
    monkeypatch.setattr(pv, "get_project", project)
    monkeypatch.setattr(pv, "list_items", items)
    monkeypatch.setattr(pv, "list_workflows", workflows)
    monkeypatch.setattr(pv, "list_views", views)
    monkeypatch.setattr(setup, "_repo_slug", lambda: "owner/repo")
    return mutated


async def test_dry_run_reports_every_change_and_mutates_nothing(fake_pv, monkeypatch, capsys):
    async def one(owner, title, **_k):
        return [{"id": "P1", "number": 4, "title": title, "closed": False}]

    monkeypatch.setattr(pv, "find_projects_by_title", one)
    code = await setup.run("Genesis Work Board", apply=False, write_config=True)
    out = capsys.readouterr().out
    assert fake_pv == [], "a dry run must not call a single mutation"
    assert (
        "would set Status options" in out
        and "would delete workflow 'Pull request linked to issue'" in out
    )
    assert "would create view 'Active'" in out and "would record project_owner" in out
    assert "PROBLEM: workflow 'Pull request merged' is missing" in out
    assert code == 1, "a missing required workflow is a non-zero exit"


async def test_a_public_project_is_refused_without_allow_public(fake_pv, monkeypatch, capsys):
    async def one(owner, title, **_k):
        return [{"id": "P1", "number": 4, "title": title, "closed": False}]

    async def public_project(owner, number, **_k):
        return pv.Project("P1", number, "Genesis Work Board", True, {})

    monkeypatch.setattr(pv, "find_projects_by_title", one)
    monkeypatch.setattr(pv, "get_project", public_project)
    assert await setup.run("Genesis Work Board", apply=True, write_config=False) == 2
    assert "REFUSING: the project is PUBLIC" in capsys.readouterr().out
    assert fake_pv == []


async def test_two_projects_with_the_title_is_a_refusal(fake_pv, monkeypatch, capsys):
    async def two(owner, title, **_k):
        return [
            {"id": "a", "number": 1, "title": title, "closed": False},
            {"id": "b", "number": 2, "title": title, "closed": False},
        ]

    monkeypatch.setattr(pv, "find_projects_by_title", two)
    assert await setup.run("Genesis Work Board", apply=True, write_config=False) == 2
    assert fake_pv == []
    assert "REFUSING" in capsys.readouterr().out


async def test_unused_unlisted_status_option_is_dropped_but_a_used_one_is_kept(
    fake_pv, monkeypatch
):
    async def one(owner, title, **_k):
        return [{"id": "P1", "number": 4, "title": title, "closed": False}]

    sent = {}

    async def capture(field_id, options, **_k):
        sent["names"] = [o["name"] for o in options]

    monkeypatch.setattr(pv, "find_projects_by_title", one)
    monkeypatch.setattr(pv, "update_single_select_options", capture)
    await setup.run("Genesis Work Board", apply=True, write_config=False)
    assert "Todo" not in sent["names"]

    async def items_using_todo(_pid, **_k):
        return {"items": [{"status": {"name": "Todo"}}], "total": 1}

    monkeypatch.setattr(pv, "list_items", items_using_todo)
    await setup.run("Genesis Work Board", apply=True, write_config=False)
    assert sent["names"][-1] == "Todo", "an option a card uses is kept"
