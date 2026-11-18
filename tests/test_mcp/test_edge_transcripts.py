"""edge_transcripts: one-call transcript lookup on the edge device (#4856)."""

from __future__ import annotations

import shlex
import types

import pytest

from genesis.mcp.health import edge_transcripts as et

# Local day 2026-11-14 in Bogota (UTC-5) runs 05:00Z on the 14th to 05:00Z on the 15th.
NAMES = [
    "20261114T040000_aaa_1.md",  # 23:00 on the 13th, local
    "20261114T150000_bbb_2.md",  # 10:00 on the 14th
    "20261115T030000_ccc_3.md",  # 22:00 on the 14th
    "20261115T060000_ddd_4.md",  # 01:00 on the 15th
    "notes.md",
]


@pytest.fixture
def edge(monkeypatch):
    calls: list[str] = []
    state = types.SimpleNamespace(rc=0, grep_out="", calls=calls)

    async def remote(cfg, command):
        calls.append(command)
        if state.rc:
            return state.rc, ""
        if " -exec grep " in command:
            return 0, state.grep_out
        return 0, "\n".join(NAMES) + "\n"

    cfg = types.SimpleNamespace(host_ip="h", host_user="u", ssh_key="k", transcripts_dir="~/talks")
    state.cfg = cfg
    monkeypatch.setattr(et, "_config", lambda: cfg)
    monkeypatch.setattr(et, "_remote", remote)
    monkeypatch.setattr("genesis.env.user_timezone", lambda: "America/Bogota")
    return state


@pytest.mark.asyncio
async def test_not_configured_without_a_transcripts_dir(edge):
    edge.cfg.transcripts_dir = None
    assert (await et._impl("status", None, None, 200))["status"] == "not configured"


@pytest.mark.asyncio
async def test_a_local_day_spans_two_utc_dates(edge):
    out = await et._impl("list", "2026-11-14", None, 200)
    assert out["files"] == ["20261114T150000_bbb_2.md", "20261115T030000_ccc_3.md"]
    # The edge is asked only for the two UTC dates the local day touches.
    assert "-name 20261114'*.md' -o -name 20261115'*.md'" in edge.calls[-1].replace(
        "'20261114*.md'", "20261114'*.md'"
    ).replace("'20261115*.md'", "20261115'*.md'")


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["2026-11-14\n", "2026-1-4", "14-11-2026", "x"])
async def test_a_malformed_date_is_refused(edge, bad):
    assert "error" in await et._impl("list", bad, None, 200)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_dir", ["-delete", "talks", "$(reboot)"])
async def test_a_directory_that_is_not_a_path_is_refused(edge, bad_dir):
    edge.cfg.transcripts_dir = bad_dir
    assert "error" in await et._impl("status", None, None, 200)
    assert edge.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query", ["budget", "a; rm -rf ~", "$(reboot)", "-e evil", "two\nlines", "it's"]
)
async def test_the_query_reaches_grep_as_one_fixed_string(edge, query):
    await et._impl("search", "2026-11-14", query, 200)
    command = edge.calls[-1]
    find_part = command.split("; ", 1)[1].split(" | head -c ")[0]
    argv = shlex.split(find_part)
    i = argv.index("-exec")
    assert argv[i + 1 : i + 9] == ["grep", "-F", "-i", "-n", "-H", "-C", "2", "-e"]
    assert argv[i + 9] == query and argv[i + 10] == "--" and argv[i + 11 :] == ["{}", "+"]
    assert argv[:2] == ["find", "-H"] and "-type" in argv  # regular files only


@pytest.mark.asyncio
async def test_a_home_relative_directory_stays_expandable(edge):
    await et._impl("status", None, None, 200)
    assert edge.calls[0].startswith('test -d "$HOME"/talks || exit 3; find -H "$HOME"/talks/ ')
    assert et._remote_path("/srv/a b") == "'/srv/a b'"


@pytest.mark.asyncio
async def test_matches_keep_only_the_day_and_are_capped(edge):
    path = "/h/talks/"
    lines = [f"{path}20261114T040000_aaa_1.md:3:yesterday's line"]
    lines += [f"{path}20261114T150000_bbb_2.md:{i}:a/b line {i}" for i in range(50)]
    lines += [f"{path}20261115T030000_ccc_3.md-9-context"]
    edge.grep_out = "\n".join(lines)
    out = await et._impl("search", "2026-11-14", "line", 10)
    assert len(out["matches"]) == 10 and "truncated" in out
    assert not any("yesterday" in m for m in out["matches"])
    every = await et._impl("search", "2026-11-14", "line", 10_000)
    assert len(every["matches"]) == 51  # 50 in-day hits + the in-day context line
    assert f"| head -c {et._REMOTE_CAP_BYTES}" in edge.calls[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize(("rc", "word"), [(124, "answer"), (3, "directory"), (255, "failed")])
async def test_a_failed_remote_call_is_an_error_not_an_empty_day(edge, rc, word):
    edge.rc = rc
    for action in ("status", "list"):
        out = await et._impl(action, "2026-11-14", None, 200)
        assert word in out["error"]
    out = await et._impl("search", "2026-11-14", "x", 200)
    assert word in out["error"]


@pytest.mark.asyncio
async def test_status_counts_the_day(edge):
    out = await et._impl("status", "2026-11-14", None, 200)
    assert out["files_that_day"] == 2 and out["latest"] == "20261115T060000_ddd_4.md"


def test_the_tool_is_registered_and_kept_from_reflection():
    from genesis.cc.session_config import _REFLECTION_READ_MCP
    from genesis.mcp.shared_http._catalog import _HEALTH_MODULES

    assert "edge_transcripts" in _HEALTH_MODULES
    assert "edge_transcripts" not in _REFLECTION_READ_MCP
