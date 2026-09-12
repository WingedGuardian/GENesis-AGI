"""Tests for recon-mcp server — findings CRUD, triage, watchlist."""

import aiosqlite
import pytest

from genesis.db.schema import create_all_tables
from genesis.mcp import recon_mcp
from genesis.mcp.recon_mcp import (
    _load_watchlist,
    init_recon_mcp,
    mcp,
)

_VERIFY_PUBLIC_REPOSITORY_IMPL = recon_mcp._verify_public_repository


@pytest.fixture
async def db():
    conn = await aiosqlite.connect(":memory:")
    conn.row_factory = aiosqlite.Row
    await create_all_tables(conn)
    yield conn
    await conn.close()


@pytest.fixture
async def tools(db):
    init_recon_mcp(db=db)
    return await mcp.get_tools()


@pytest.fixture(autouse=True)
def public_github_repository(monkeypatch):
    async def public(repository):
        return True, {
            "full_name": repository, "private": False, "visibility": "public",
        }, None

    monkeypatch.setattr("genesis.mcp.recon_mcp._verify_public_repository", public)


# ── watchlist (unchanged) ────────────────────────────────────────────────────


async def test_all_tools_registered(tools):
    for name in [
        "recon_config",
        "recon_findings",
        "recon_triage",
        "recon_store_finding",
        "recon_github_search",
        "recon_github_read",
    ]:
        assert name in tools, f"Missing tool: {name}"


async def test_github_public_api_uses_fixed_unauthenticated_transport(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        async def aiter_raw(self, *, chunk_size):
            assert chunk_size == 64 * 1024
            yield b'{"ok":true}'

    class Stream:
        async def __aenter__(self):
            return Response()

        async def __aexit__(self, *_args):
            return None

    class Client:
        def __init__(self, **kwargs):
            captured["client"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, method, url, **kwargs):
            captured["request"] = (method, url, kwargs)
            return Stream()

    monkeypatch.setattr("genesis.mcp.recon_mcp.httpx.AsyncClient", Client)
    ok, raw = await recon_mcp._github_public_api(
        "search/repositories", params={"q": "topic"}
    )

    assert (ok, raw) == (True, '{"ok":true}')
    assert captured["request"] == (
        "GET",
        "https://api.github.com/search/repositories",
        {"params": {"q": "topic"}},
    )
    headers = captured["client"]["headers"]
    assert all(name.lower() != "authorization" for name in headers)
    assert headers["Accept-Encoding"] == "identity"
    assert captured["client"]["follow_redirects"] is False


async def test_github_public_api_rejects_oversized_response(monkeypatch):
    class Response:
        status_code = 200

        async def aiter_raw(self, *, chunk_size):
            assert chunk_size == 64 * 1024
            yield b"12345"

    class Stream:
        async def __aenter__(self):
            return Response()

        async def __aexit__(self, *_args):
            return None

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            return Stream()

    monkeypatch.setattr("genesis.mcp.recon_mcp.httpx.AsyncClient", Client)
    ok, failure = await recon_mcp._github_public_api("repos/o/r", max_bytes=4)
    assert ok is False
    assert failure == recon_mcp._GitHubAPIFailure("response_too_large")


@pytest.mark.parametrize(
    "status,headers,kind",
    [
        (403, {}, "forbidden_or_rate_limited"),
        (403, {"x-ratelimit-remaining": "0"}, "rate_limited"),
        (403, {"retry-after": "60"}, "rate_limited"),
        (429, {}, "rate_limited"),
        (404, {}, "not_found"),
        (422, {}, "invalid_request"),
        (500, {}, "http_error"),
    ],
)
async def test_github_public_api_preserves_http_failure_kind(
    monkeypatch, status, headers, kind,
):
    class Response:
        status_code = status

        def __init__(self):
            self.headers = headers

    class Stream:
        async def __aenter__(self):
            return Response()

        async def __aexit__(self, *_args):
            return None

    class Client:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def stream(self, *_args, **_kwargs):
            return Stream()

    monkeypatch.setattr("genesis.mcp.recon_mcp.httpx.AsyncClient", Client)
    ok, failure = await recon_mcp._github_public_api("repos/o/r")
    assert ok is False
    assert failure == recon_mcp._GitHubAPIFailure(kind, status)


async def test_github_search_distinguishes_empty_results_from_failure(tools, monkeypatch):
    async def empty(*args, **kwargs):
        return True, '{"total_count":0,"incomplete_results":false,"items":[]}'

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", empty)
    result = await tools["recon_github_search"].fn(kind="repositories", query="unlikely")
    assert result == {
        "ok": True,
        "kind": "repositories",
        "query": "unlikely",
        "total_count": 0,
        "accessible_count": 0,
        "incomplete_results": False,
        "items": [],
        "visibility_filter_applied": True,
        "page": 1,
        "per_page": 30,
        "has_more": False,
    }

    async def failed(*args, **kwargs):
        return False, ""

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", failed)
    failed_result = await tools["recon_github_search"].fn(
        kind="repositories", query="unlikely"
    )
    assert failed_result["ok"] is False
    assert "failed" in failed_result["error"].lower()


async def test_github_search_reports_invalid_request_distinctly(tools, monkeypatch):
    async def rejected(*_args, **_kwargs):
        return False, recon_mcp._GitHubAPIFailure("invalid_request", 422)

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", rejected)
    result = await tools["recon_github_search"].fn(
        kind="repositories", query="malformed",
    )
    assert result == {
        "ok": False,
        "error": "GitHub search rejected the request; revise the query, path, or ref",
    }


async def test_github_read_file_reports_truncation(tools, monkeypatch):
    async def fake(*args, **kwargs):
        import base64
        import json

        return True, json.dumps({
            "type": "file",
            "size": 12,
            "sha": "abc",
            "html_url": "https://github.com/o/r/blob/main/a.txt",
            "download_url": "https://raw.example/a.txt",
            "encoding": "base64",
            "content": base64.b64encode(b"abcdefghijkl").decode(),
        })

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", fake)
    result = await tools["recon_github_read"].fn(
        repository="o/r", operation="file", path="a.txt", max_chars=5
    )
    assert result["ok"] is True
    assert result["content"] == "abcde"
    assert result["truncated"] is True
    assert result["total_chars"] == 12


async def test_github_file_read_rejects_directory_response_without_crashing(tools, monkeypatch):
    async def directory(*args, **kwargs):
        return True, '[{"type":"file","name":"child.txt"}]'

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", directory)
    result = await tools["recon_github_read"].fn(
        repository="o/r", operation="file", path="directory"
    )
    assert result["ok"] is False
    assert "not a file" in result["error"]
    assert result["metadata"] == {"response_type": "list"}


async def test_github_file_error_does_not_echo_content(tools, monkeypatch):
    import base64
    import json

    async def binary(*args, **kwargs):
        return True, json.dumps({
            "type": "file",
            "name": "binary.dat",
            "size": 100_000,
            "encoding": "base64",
            "content": base64.b64encode(b"\xff" * 100_000).decode(),
        })

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", binary)
    result = await tools["recon_github_read"].fn(
        repository="o/r", operation="file", path="binary.dat", max_chars=5,
    )

    assert result["ok"] is False
    assert result["metadata"] == {
        "name": "binary.dat", "size": 100_000, "type": "file", "encoding": "base64",
    }
    assert len(json.dumps(result)) < 500


async def test_github_file_rejects_invalid_base64(tools, monkeypatch):
    async def invalid(*args, **kwargs):
        return True, '{"type":"file","size":3,"encoding":"base64","content":"%%%"}'

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", invalid)
    result = await tools["recon_github_read"].fn(
        repository="o/r", operation="file", path="bad.txt",
    )
    assert result["ok"] is False
    assert "UTF-8" in result["error"]


async def test_github_search_has_more_respects_api_1000_result_cap(tools, monkeypatch):
    async def many(*args, **kwargs):
        return True, '{"total_count":5000,"incomplete_results":false,"items":[]}'

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", many)
    result = await tools["recon_github_search"].fn(
        kind="repositories", query="popular", page=10, per_page=100
    )
    assert result["has_more"] is False
    assert result["accessible_count"] == 1000


async def test_github_search_rejects_nul_query_before_transport(tools, monkeypatch):
    async def unexpected(*_args, **_kwargs):
        raise AssertionError("transport must not run")

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", unexpected)
    result = await tools["recon_github_search"].fn(
        kind="repositories", query="topic\x00is:private"
    )
    assert result == {"ok": False, "error": "query must not contain NUL bytes"}


@pytest.mark.parametrize(
    "payload",
    ["[]", "null", "{}", '{"total_count":null,"items":[]}'],
)
async def test_github_search_rejects_malformed_payloads(tools, monkeypatch, payload):
    async def malformed(*args, **kwargs):
        return True, payload

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", malformed)
    result = await tools["recon_github_search"].fn(kind="repositories", query="topic")
    assert result["ok"] is False


async def test_github_search_forces_public_visibility_and_filters_private_items(tools, monkeypatch):
    calls = []

    async def fake(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return True, '{"total_count":2,"items":[{"full_name":"o/public","private":false,"visibility":"public"},{"full_name":"o/private","private":true,"visibility":"private"}]}'

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", fake)
    result = await tools["recon_github_search"].fn(
        kind="repositories", query="topic is:private"
    )

    assert calls[0][0] == "search/repositories"
    assert calls[0][1]["params"]["q"] == "topic is:public"
    assert [item["full_name"] for item in result["items"]] == ["o/public"]
    assert result["visibility_filter_applied"] is True


@pytest.mark.parametrize(
    "repository",
    [
        {"full_name": "o/r", "private": False},
        {"full_name": "o/r", "private": False, "visibility": None},
        {"full_name": "o/r", "private": False, "visibility": "unknown"},
        {"full_name": "o/r", "private": True, "visibility": "public"},
    ],
)
async def test_github_repository_search_rejects_invalid_visibility_evidence(
    tools, monkeypatch, repository,
):
    import json

    async def malformed(*args, **kwargs):
        return True, json.dumps({"total_count": 1, "items": [repository]})

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", malformed)
    result = await tools["recon_github_search"].fn(kind="repositories", query="topic")
    assert result == {
        "ok": False,
        "error": "GitHub repository search could not verify repository visibility",
    }


async def test_github_issue_search_accepts_public_api_repository_identities(tools, monkeypatch):
    import json

    async def fake_search(*args, **kwargs):
        return True, json.dumps({
            "total_count": 2,
            "items": [
                {"title": "public", "repository_url": "https://api.github.com/repos/o/public"},
                {"title": "second", "repository_url": "https://api.github.com/repos/o/second"},
            ],
        })

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", fake_search)
    result = await tools["recon_github_search"].fn(kind="issues", query="bug")

    assert [item["title"] for item in result["items"]] == ["public", "second"]
    assert result["visibility_filter_applied"] is True


async def test_github_issue_search_forces_public_visibility(tools, monkeypatch):
    calls = []

    async def fake_search(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return True, '{"total_count":0,"items":[]}'

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", fake_search)
    result = await tools["recon_github_search"].fn(
        kind="issues", query="bug is:private",
    )

    assert result["ok"] is True
    assert calls[0][0] == "search/issues"
    assert calls[0][1]["params"]["q"] == "bug is:issue is:public"


async def test_github_issue_search_replaces_type_qualifiers(
    tools, monkeypatch,
):
    calls = []

    async def fake_search(endpoint, **kwargs):
        calls.append((endpoint, kwargs))
        return True, '{"total_count":0,"items":[]}'

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", fake_search)
    result = await tools["recon_github_search"].fn(
        kind="issues", query="label:bug is:pr type:pr",
    )

    assert result["ok"] is True
    assert calls[0][1]["params"]["q"] == "label:bug is:issue is:public"


async def test_github_issue_search_rejects_boolean_or_before_transport(
    tools, monkeypatch,
):
    async def unexpected(*_args, **_kwargs):
        raise AssertionError("Boolean OR issue query must not reach transport")

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", unexpected)
    result = await tools["recon_github_search"].fn(
        kind="issues", query="label:bug OR label:docs",
    )
    assert result == {
        "ok": False,
        "error": "issues query must not contain Boolean OR; run separate searches",
    }


async def test_github_issue_search_rejects_type_only_query(tools, monkeypatch):
    async def unexpected(*_args, **_kwargs):
        raise AssertionError("type-only issue query must not reach transport")

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", unexpected)
    result = await tools["recon_github_search"].fn(kind="issues", query="is:pr")
    assert result == {
        "ok": False,
        "error": "query must include terms beyond visibility/type qualifiers",
    }


async def test_github_issue_search_rejects_pull_request_response(tools, monkeypatch):
    async def malformed(*args, **kwargs):
        return True, (
            '{"total_count":1,"items":[{'
            '"repository_url":"https://api.github.com/repos/o/public",'
            '"pull_request":{}}]}'
        )

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", malformed)
    result = await tools["recon_github_search"].fn(kind="issues", query="bug")
    assert result == {
        "ok": False,
        "error": "GitHub issues search unexpectedly returned a pull request",
    }


async def test_github_search_rejects_credentialed_code_endpoint_before_transport(
    tools, monkeypatch,
):
    async def unexpected(*_args, **_kwargs):
        raise AssertionError("credentialed code-search transport must not run")

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", unexpected)
    result = await tools["recon_github_search"].fn(kind="code", query="symbol")
    assert result == {"ok": False, "error": "kind must be repositories or issues"}


async def test_internal_repository_is_not_public(tools, monkeypatch):
    async def internal(*args, **kwargs):
        return True, '{"private":false,"visibility":"internal","full_name":"o/internal"}'

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", internal)
    verified, metadata, failure = await _VERIFY_PUBLIC_REPOSITORY_IMPL("o/internal")
    assert verified is True
    assert metadata is None
    assert failure is None


@pytest.mark.parametrize(
    "metadata",
    [
        '{"private":false}',
        '{"private":false,"visibility":null}',
        '{"private":false,"visibility":"unknown"}',
        '{"private":true,"visibility":"public"}',
    ],
)
async def test_repository_visibility_rejects_malformed_schema(monkeypatch, metadata):
    async def malformed(*args, **kwargs):
        return True, metadata

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", malformed)
    verified, public_metadata, failure = await _VERIFY_PUBLIC_REPOSITORY_IMPL("o/r")
    assert verified is False
    assert public_metadata is None
    assert failure == recon_mcp._GitHubAPIFailure("invalid_response")


async def test_issue_search_rejects_missing_repository_identity(tools, monkeypatch):
    async def malformed(*args, **kwargs):
        return True, '{"total_count":1,"items":[{"repository_url":null}]}'

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", malformed)
    result = await tools["recon_github_search"].fn(kind="issues", query="bug")
    assert result == {
        "ok": False,
        "error": "GitHub issues search returned an invalid repository identity",
    }


async def test_issue_search_rejects_non_github_com_repository_url(tools, monkeypatch):
    async def enterprise(*args, **kwargs):
        return True, '{"total_count":1,"items":[{"repository_url":"https://github.example/api/v3/repos/o/public"}]}'

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", enterprise)
    result = await tools["recon_github_search"].fn(kind="issues", query="bug")
    assert result == {
        "ok": False,
        "error": "GitHub issues search returned an invalid repository identity",
    }


async def test_github_read_rejects_non_public_repository(tools, monkeypatch):
    async def private(_repository):
        return True, None, None

    monkeypatch.setattr("genesis.mcp.recon_mcp._verify_public_repository", private)
    result = await tools["recon_github_read"].fn(repository="o/private")

    assert result == {"ok": False, "error": "repository must exist and be public"}


async def test_github_read_reports_visibility_verification_failure(tools, monkeypatch):
    async def failed(_repository):
        return False, None, recon_mcp._GitHubAPIFailure("network")

    monkeypatch.setattr("genesis.mcp.recon_mcp._verify_public_repository", failed)
    result = await tools["recon_github_read"].fn(repository="o/public")

    assert result == {
        "ok": False,
        "error": "GitHub repository visibility check failed because of a network error",
    }


@pytest.mark.parametrize("field,value", [("path", "a\x00.txt"), ("ref", "main\x00other")])
async def test_github_read_rejects_nul_before_transport(tools, monkeypatch, field, value):
    async def unexpected(*_args, **_kwargs):
        raise AssertionError("visibility check must not run")

    monkeypatch.setattr("genesis.mcp.recon_mcp._verify_public_repository", unexpected)
    args = {"repository": "o/r", "operation": "file", "path": "a.txt", field: value}
    result = await tools["recon_github_read"].fn(**args)
    assert result == {"ok": False, "error": "path and ref must not contain NUL bytes"}


async def test_github_tree_caps_local_response(tools, monkeypatch):
    import json

    async def fake(*args, **kwargs):
        return True, json.dumps({"tree": [{"path": str(i)} for i in range(2001)], "truncated": False})

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", fake)
    result = await tools["recon_github_read"].fn(repository="o/r", operation="tree")
    tree_result = result["result"]
    assert len(tree_result["tree"]) == 2000
    assert tree_result["truncated"] is True
    assert tree_result["local_truncated"] is True
    assert tree_result["upstream_truncated"] is False
    assert tree_result["received_entries"] == 2001


async def test_github_large_file_uses_blob_fallback(tools, monkeypatch):
    import base64
    import json

    responses = iter([
        {"type": "file", "size": 10, "sha": "abc", "encoding": "none", "content": ""},
        {"size": 10, "encoding": "base64", "content": base64.b64encode(b"large file").decode()},
    ])

    async def fake(*args, **kwargs):
        return True, json.dumps(next(responses))

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", fake)
    result = await tools["recon_github_read"].fn(
        repository="o/r", operation="file", path="large.txt", max_chars=5,
    )

    assert result["ok"] is True
    assert result["content"] == "large"
    assert result["truncated"] is True


async def test_github_file_over_8_mib_does_not_fetch_blob(tools, monkeypatch):
    import json

    calls = 0

    async def fake(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return True, json.dumps({
            "type": "file",
            "size": 8 * 1024 * 1024 + 1,
            "sha": "abc",
            "encoding": "none",
        })

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", fake)
    result = await tools["recon_github_read"].fn(
        repository="o/r", operation="file", path="large.txt",
    )
    assert result["ok"] is False
    assert "8 MiB" in result["error"]
    assert calls == 1


@pytest.mark.parametrize("encoding", ["base64", "none"])
async def test_github_file_enforces_decoded_limit_for_inline_and_blob(
    tools, monkeypatch, encoding,
):
    import json

    responses = [{
        "type": "file", "size": 0, "sha": "abc", "encoding": encoding,
        "content": "YQ==" if encoding == "base64" else "",
    }]
    if encoding == "none":
        responses.append({"size": 0, "encoding": "base64", "content": "YQ=="})
    response_iter = iter(responses)

    async def fake(*_args, **_kwargs):
        return True, json.dumps(next(response_iter))

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", fake)
    monkeypatch.setattr("genesis.mcp.recon_mcp._GITHUB_CONTENTS_MAX_BYTES", 0)
    result = await tools["recon_github_read"].fn(
        repository="o/r", operation="file", path="large.txt",
    )
    assert result["ok"] is False
    assert "8 MiB" in result["error"]


@pytest.mark.parametrize("size", [True, -1])
async def test_github_file_rejects_invalid_size_metadata(tools, monkeypatch, size):
    import json

    async def fake(*_args, **_kwargs):
        return True, json.dumps({
            "type": "file", "size": size, "encoding": "base64", "content": "",
        })

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", fake)
    result = await tools["recon_github_read"].fn(
        repository="o/r", operation="file", path="bad.txt",
    )
    assert result["ok"] is False
    assert "invalid size metadata" in result["error"]


@pytest.mark.parametrize("blob", ["[]", "null", '{"encoding":"base64","content":null}'])
async def test_github_large_file_rejects_malformed_blob(tools, monkeypatch, blob):
    import json

    responses = iter([
        json.dumps({"type": "file", "size": 2_000_000, "sha": "abc", "encoding": "none"}),
        blob,
    ])

    async def fake(*args, **kwargs):
        return True, next(responses)

    monkeypatch.setattr("genesis.mcp.recon_mcp._github_public_api", fake)
    result = await tools["recon_github_read"].fn(
        repository="o/r", operation="file", path="large.txt",
    )
    assert result["ok"] is False


def test_load_watchlist():
    projects = _load_watchlist()
    assert isinstance(projects, list)
    assert len(projects) >= 5
    names = [p["name"] for p in projects]
    assert "Claude Code" in names


def test_load_watchlist_has_required_fields():
    projects = _load_watchlist()
    for p in projects:
        assert "name" in p
        assert "repo" in p
        assert "track" in p
        assert "priority" in p


async def test_recon_config_watchlist_returns_all(tools):
    result = await tools["recon_config"].fn(aspect="watchlist")
    assert len(result) >= 5


async def test_recon_config_watchlist_filters_by_priority(tools):
    high = await tools["recon_config"].fn(aspect="watchlist", priority="high")
    assert all(p["priority"] == "high" for p in high)
    assert len(high) >= 1


# ── findings CRUD ────────────────────────────────────────────────────────────


async def test_store_and_query_roundtrip(tools):
    result = await tools["recon_store_finding"].fn(
        title="Test Finding",
        summary="Some details",
        job_type="github_landscape",
        priority="high",
    )
    assert "finding_id" in result

    findings = await tools["recon_findings"].fn()
    assert len(findings) == 1
    assert findings[0]["category"] == "github_landscape"
    assert findings[0]["priority"] == "high"
    assert "Test Finding" in findings[0]["content"]


async def test_findings_filter_by_job_type(tools):
    await tools["recon_store_finding"].fn(
        title="A", summary="", job_type="github_landscape", priority="medium",
    )
    await tools["recon_store_finding"].fn(
        title="B", summary="", job_type="email_recon", priority="medium",
    )

    gh = await tools["recon_findings"].fn(job_type="github_landscape")
    assert len(gh) == 1
    assert "A" in gh[0]["content"]

    email = await tools["recon_findings"].fn(job_type="email_recon")
    assert len(email) == 1
    assert "B" in email[0]["content"]


async def test_findings_filter_by_priority(tools):
    await tools["recon_store_finding"].fn(
        title="High", summary="", job_type="web_monitoring", priority="high",
    )
    await tools["recon_store_finding"].fn(
        title="Low", summary="", job_type="web_monitoring", priority="low",
    )

    high = await tools["recon_findings"].fn(priority="high")
    assert all(r["priority"] == "high" for r in high)


async def test_findings_filter_by_triaged(tools):
    result = await tools["recon_store_finding"].fn(
        title="Triage me", summary="", job_type="test", priority="medium",
    )
    fid = result["finding_id"]

    untriaged = await tools["recon_findings"].fn(triaged=False)
    assert len(untriaged) == 1

    await tools["recon_triage"].fn(finding_id=fid, notes="done", action="dismiss")

    untriaged = await tools["recon_findings"].fn(triaged=False)
    assert len(untriaged) == 0

    triaged = await tools["recon_findings"].fn(triaged=True)
    assert len(triaged) == 1


# ── triage ───────────────────────────────────────────────────────────────────


async def test_triage_dismiss(tools):
    result = await tools["recon_store_finding"].fn(
        title="Dismiss me", summary="", job_type="test", priority="low",
    )
    fid = result["finding_id"]

    triage = await tools["recon_triage"].fn(finding_id=fid, notes="not relevant", action="dismiss")
    assert triage["success"] is True
    assert triage["action"] == "dismiss"


async def test_triage_acknowledge(tools):
    result = await tools["recon_store_finding"].fn(
        title="Ack me", summary="", job_type="test", priority="medium",
    )
    fid = result["finding_id"]

    triage = await tools["recon_triage"].fn(finding_id=fid, notes="noted", action="acknowledge")
    assert triage["success"] is True
    assert triage["action"] == "acknowledge"


async def test_triage_defer(tools):
    result = await tools["recon_store_finding"].fn(
        title="Defer me", summary="", job_type="test", priority="medium",
    )
    fid = result["finding_id"]

    triage = await tools["recon_triage"].fn(finding_id=fid, notes="check later", action="defer")
    assert triage["success"] is True
    assert triage["action"] == "defer"

    # Should still be untriaged (not resolved)
    untriaged = await tools["recon_findings"].fn(triaged=False)
    assert any(r["id"] == fid for r in untriaged)


async def test_triage_invalid_action(tools):
    result = await tools["recon_store_finding"].fn(
        title="Invalid", summary="", job_type="test", priority="medium",
    )
    fid = result["finding_id"]

    triage = await tools["recon_triage"].fn(finding_id=fid, notes="", action="delete")
    assert triage["success"] is False
    assert "Invalid action" in triage["error"]


async def test_store_finding_with_source_url(tools):
    await tools["recon_store_finding"].fn(
        title="URL Finding",
        summary="Details",
        job_type="web_monitoring",
        source_url="https://example.com",
    )
    findings = await tools["recon_findings"].fn()
    assert "https://example.com" in findings[0]["content"]
