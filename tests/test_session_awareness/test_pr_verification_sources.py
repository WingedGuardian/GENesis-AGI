"""Source packets must account for every PR path before they can be reviewed."""

import hashlib
import subprocess

import httpx
import pytest

from genesis.session_awareness.pr_verification_sources import SourceError, reconcile_diff

# The blob id of "after\n", which patch() writes as the post-image.
AFTER_BLOB = hashlib.sha1(b"blob 6\0after\n").hexdigest()


def patch(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "ordinary.txt").write_text("before\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "fixture",
        ],
        check=True,
    )
    (tmp_path / "ordinary.txt").write_text("after\n")
    return subprocess.check_output(["git", "-C", str(tmp_path), "diff"])


def test_complete_diff_reconciles_actual_git_patch(tmp_path):
    raw = patch(tmp_path)
    reconcile_diff(raw, [dict(filename="ordinary.txt", additions=1, deletions=1)], timeout=5)


@pytest.mark.parametrize("kind", ["empty", "add-delete"])
def test_real_git_empty_added_and_deleted_paths(tmp_path, kind):
    patch(tmp_path)
    expected = []
    if kind == "empty":
        raw = subprocess.check_output(["git", "-C", str(tmp_path), "diff", "HEAD", "HEAD"])
    else:
        (tmp_path / "ordinary.txt").unlink()
        (tmp_path / "added\t雪.txt").write_text("new line\n")
        subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
        raw = subprocess.check_output(["git", "-C", str(tmp_path), "diff", "--cached"])
        expected = [
            dict(filename="ordinary.txt", additions=0, deletions=1),
            dict(filename="added\t雪.txt", additions=1, deletions=0),
        ]
    reconcile_diff(raw, expected, timeout=5)


@pytest.mark.parametrize(
    "files",
    [
        [],
        [dict(filename="other.txt", additions=1, deletions=1)],
        [dict(filename="ordinary.txt", additions=2, deletions=1)],
    ],
)
def test_incomplete_or_wrong_patch_cannot_be_ready(tmp_path, files):
    with pytest.raises(SourceError):
        reconcile_diff(patch(tmp_path), files, timeout=5)


def source_fixture(tmp_path):
    import json

    from genesis.session_awareness.pr_verification_sources import acquire_sources

    raw = patch(tmp_path)
    metadata = dict(
        merged=True,
        body="fixture promise",
        changed_files=1,
        merge_commit_sha="a" * 40,
        head=dict(sha="b" * 40),
        base=dict(sha="c" * 40),
    )
    entries = [
        dict(filename="ordinary.txt", additions=1, deletions=1, status="modified", sha=AFTER_BLOB)
    ]
    replies = [
        json.dumps(metadata).encode(),
        raw,
        json.dumps(entries).encode(),
        json.dumps(metadata).encode(),
    ]
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, stream=httpx.ByteStream(replies.pop(0)))

    def acquire(repo, pr, **kwargs):
        return acquire_sources(
            repo,
            pr,
            transport=httpx.MockTransport(respond),
            run=lambda *a, **kw: b"fixture-token\n",
        )

    return acquire, metadata, entries, replies, calls, None


def test_acquisition_brackets_sources_and_packet_is_exclusive(tmp_path):
    import json

    from genesis.session_awareness.pr_verification_sources import digest, save_packet

    acquire, _, _, replies, calls, run = source_fixture(tmp_path)
    snapshot = acquire("owner/repo", 7, run=run)
    assert replies == [] and len(calls) == 4
    assert snapshot.files[0]["filename"] == "ordinary.txt"
    assert all(str(request.url).startswith("https://api.github.com/") for request in calls)
    output = tmp_path / "packet"
    result = save_packet(snapshot, output)
    packet = json.loads(result.read_text())
    assert packet["body_sha256"] == digest(b"fixture promise")
    assert packet["diff_sha256"] == digest(snapshot.diff.encode())
    assert output.stat().st_mode & 0o777 == 0o700
    with pytest.raises(FileExistsError):
        save_packet(snapshot, output)


@pytest.mark.parametrize(
    "change", ["body", "merge_commit_sha", "changed_files", "truncated", "duplicate"]
)
def test_acquisition_refuses_changed_or_incomplete_sources(tmp_path, change):
    import json

    acquire, metadata, entries, replies, _, run = source_fixture(tmp_path)
    if change == "truncated":
        replies[2] = b"[]"
    elif change == "duplicate":
        metadata["changed_files"] = 2
        replies[0] = json.dumps(metadata).encode()
        replies[2] = json.dumps(entries * 2).encode()
    else:
        metadata[change] = {"body": "revised", "merge_commit_sha": "e" * 40, "changed_files": 2}[
            change
        ]
        replies[3] = json.dumps(metadata).encode()
    with pytest.raises(SourceError):
        acquire("owner/repo", 7, run=run)


@pytest.mark.parametrize("repo,pr", [("--help", 1), ("owner/repo", True), ("owner/repo", 0)])
def test_target_validation_precedes_subprocess(repo, pr):
    from genesis.session_awareness.pr_verification_sources import acquire_sources

    def forbidden(*args, **kwargs):
        pytest.fail("invalid target reached subprocess")

    with pytest.raises(SourceError):
        acquire_sources(repo, pr, run=forbidden)


def test_runner_enforces_real_byte_and_time_limits():
    import sys

    from genesis.session_awareness.pr_verification_sources import _run

    assert _run([sys.executable, "-c", "print('ok')"], timeout=5, limit=8) == b"ok\n"
    with pytest.raises(SourceError, match="byte limit"):
        _run([sys.executable, "-c", "print('x'*10000)"], timeout=5, limit=8)
    with pytest.raises(SourceError, match="deadline"):
        _run([sys.executable, "-c", "import time;time.sleep(10)"], timeout=0.1, limit=8)


@pytest.mark.parametrize("count", [0, 1, 99, 100, 101, 3000, 3001])
def test_page_boundaries_reconcile_reported_denominator(monkeypatch, count):
    import json

    from genesis.session_awareness import pr_verification_sources as sources

    entries = [
        dict(filename=f"file-{i}", additions=0, deletions=0, status="modified", sha="d" * 40)
        for i in range(count)
    ]
    metadata = dict(
        merged=True,
        body=None,
        changed_files=count,
        merge_commit_sha="a" * 40,
        head=dict(sha="b" * 40),
        base=dict(sha="c" * 40),
    )
    pages = []

    def respond(request):
        path = str(request.url)
        if "/files?" in path:
            page = int(path.split("page=")[-1])
            pages.append(page)
            raw = json.dumps(entries[(page - 1) * 100 : page * 100]).encode()
        elif request.headers["accept"] == "application/vnd.github.diff":
            raw = b"fixture diff"
        else:
            raw = json.dumps(metadata).encode()
        return httpx.Response(200, stream=httpx.ByteStream(raw))

    def acquire():
        return sources.acquire_sources(
            "owner/repo", 7, transport=httpx.MockTransport(respond), run=lambda *a, **kw: b"fixture"
        )

    checked = []
    monkeypatch.setattr(sources, "reconcile_diff", lambda raw, files, **kw: checked.append(files))
    if count > 3000:
        with pytest.raises(SourceError, match="maximum 3000"):
            acquire()
        assert pages == [] and checked == []
    else:
        result = acquire()
        assert result.body == "" and len(result.files) == count
        assert pages == list(range(1, max(1, (count + 99) // 100) + 1))
        assert len(checked) == 1 and len(checked[0]) == count


@pytest.mark.parametrize("value", [False, "true", 1, None])
def test_merged_flag_is_not_coerced(value):
    from genesis.session_awareness.pr_verification_sources import _metadata

    with pytest.raises(SourceError, match="not merged"):
        _metadata(dict(merged=value))


def test_real_git_binary_mode_and_rename_paths(tmp_path):
    patch(tmp_path)
    (tmp_path / "binary.dat").write_bytes(b"\x00old\xff")
    (tmp_path / "mode.sh").write_text("fixture\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.test",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "commit.gpgsign=false",
            "commit",
            "-qm",
            "binary fixture",
        ],
        check=True,
    )
    (tmp_path / "ordinary.txt").rename(tmp_path / "renamed\tline\n雪.txt")
    (tmp_path / "binary.dat").write_bytes(b"\x00new\xff")
    (tmp_path / "mode.sh").chmod(0o755)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    raw = subprocess.check_output(
        ["git", "-C", str(tmp_path), "diff", "--cached", "--binary", "-M"]
    )
    reconcile_diff(
        raw,
        [
            dict(filename="binary.dat", additions=0, deletions=0),
            dict(filename="mode.sh", additions=0, deletions=0),
            dict(
                filename="renamed\tline\n雪.txt",
                additions=0,
                deletions=0,
                previous_filename="ordinary.txt",
            ),
        ],
        timeout=5,
    )


def mode_patch(tmp_path, *, content: bool):
    patch(tmp_path)
    subprocess.run(["git", "-C", str(tmp_path), "checkout", "-q", "--", "."], check=True)
    (tmp_path / "ordinary.txt").chmod(0o755)
    if content:
        (tmp_path / "ordinary.txt").write_text("after\n")
    return subprocess.check_output(["git", "-C", str(tmp_path), "diff"])


@pytest.mark.parametrize("content", [False, True])
@pytest.mark.parametrize("dropped", [b"old mode 100644\n", b"new mode 100755\n"])
def test_unpaired_mode_header_is_an_incomplete_diff(tmp_path, content, dropped):
    # Git's numstat still reports the path with either half missing (git 2.43),
    # so the census alone cannot see that the new or old mode was lost.
    raw = mode_patch(tmp_path, content=content)
    assert dropped in raw
    counts = (1, 1) if content else (0, 0)
    files = [dict(filename="ordinary.txt", additions=counts[0], deletions=counts[1])]
    reconcile_diff(raw, files, timeout=5)  # the complete pair is accepted
    with pytest.raises(SourceError, match="mode"):
        reconcile_diff(raw.replace(dropped, b"", 1), files, timeout=5)


def test_mode_halves_from_different_files_do_not_pair(tmp_path):
    # Two mode-only files: drop "new mode" from the first block and "old mode"
    # from the second. Each block is then an orphan, though one of each remains.
    patch(tmp_path)
    (tmp_path / "ordinary.txt").write_text("before\n")
    (tmp_path / "second.txt").write_text("fixture\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    for name in ("ordinary.txt", "second.txt"):
        (tmp_path / name).chmod(0o755)
    raw = subprocess.check_output(["git", "-C", str(tmp_path), "diff"])
    first, second = raw.split(b"diff --git ")[1:]
    assert b"ordinary.txt" in first and b"second.txt" in second
    split = (
        b"diff --git "
        + first.replace(b"new mode 100755\n", b"")
        + b"diff --git "
        + second.replace(b"old mode 100644\n", b"")
    )
    files = [
        dict(filename="ordinary.txt", additions=0, deletions=0),
        dict(filename="second.txt", additions=0, deletions=0),
    ]
    reconcile_diff(raw, files, timeout=5)  # both complete pairs are accepted
    with pytest.raises(SourceError, match="mode"):
        reconcile_diff(split, files, timeout=5)


def test_patch_parser_ignores_caller_subdirectory_and_git_overrides(tmp_path, monkeypatch):
    raw = patch(tmp_path)
    nested = tmp_path / "scripts"
    nested.mkdir()
    monkeypatch.chdir(nested)
    monkeypatch.setenv("GIT_DIR", str(tmp_path / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(nested))
    monkeypatch.setenv("GIT_PREFIX", "scripts/")
    reconcile_diff(raw, [dict(filename="ordinary.txt", additions=1, deletions=1)], timeout=5)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_runner_bounds_both_streams(stream):
    import sys

    from genesis.session_awareness.pr_verification_sources import _run

    with pytest.raises(SourceError, match="byte limit"):
        _run(
            [sys.executable, "-c", f"import sys;sys.{stream}.write('x'*10000)"], timeout=5, limit=8
        )


def test_runner_handles_large_input_while_draining_output():
    import sys

    from genesis.session_awareness.pr_verification_sources import _run

    result = _run(
        [
            sys.executable,
            "-c",
            "import sys;sys.stderr.write('start');print(len(sys.stdin.buffer.read()))",
        ],
        timeout=5,
        limit=128,
        data=b"x" * 200000,
    )
    assert result == b"200000\n"


def test_http_source_preserves_control_characters_and_literal_carets(tmp_path, monkeypatch):
    import json

    from genesis.session_awareness.pr_verification_sources import digest, save_packet

    acquire, metadata, _, replies, calls, _ = source_fixture(tmp_path)
    metadata["body"] = "promise\x1b[31m\x85end; literal ^[[31m^Eend; 雪"
    replies[0] = replies[3] = json.dumps(metadata).encode()
    monkeypatch.setenv("GH_HOST", "alternate.example.test")
    snapshot = acquire("owner/repo", 7)
    packet = json.loads(save_packet(snapshot, tmp_path / "packet").read_text())
    assert packet["body"] == metadata["body"]
    assert packet["body_sha256"] == digest(metadata["body"].encode())
    assert all(request.url.host == "api.github.com" for request in calls)
    assert all(request.headers["accept-encoding"] == "identity" for request in calls)


@pytest.mark.parametrize("credential", [b"", b"\n", b"space inside", b"bad\x1b", b"bad\xff"])
def test_invalid_credential_never_reaches_http(credential):
    from genesis.session_awareness.pr_verification_sources import acquire_sources

    def forbidden(request):
        pytest.fail("invalid credential reached HTTP")

    with pytest.raises(SourceError, match="credential"):
        acquire_sources(
            "owner/repo",
            7,
            run=lambda *a, **kw: credential,
            transport=httpx.MockTransport(forbidden),
        )


def test_credential_lookup_is_host_pinned_and_failure_is_private():
    from genesis.session_awareness.pr_verification_sources import acquire_sources

    commands = []
    token = b"private-fixture-token"

    def run(argv, **kwargs):
        commands.append((argv, kwargs))
        return token

    def fail(request):
        assert request.headers["authorization"] == "Bearer " + token.decode()
        raise httpx.ConnectError(token.decode(), request=request)

    with pytest.raises(SourceError) as error:
        acquire_sources("owner/repo", 7, run=run, transport=httpx.MockTransport(fail))
    assert str(error.value) == "GitHub source request failed"
    assert commands[0][0] == ["gh", "auth", "token", "--hostname", "github.com"]
    assert token.decode() not in repr(commands)
    assert commands[0][1]["limit"] == 16384


@pytest.mark.parametrize("kind", ["redirect", "encoding", "metadata-cap", "diff-cap", "deadline"])
def test_http_refusals_close_streams_and_never_publish(tmp_path, monkeypatch, kind):
    import asyncio
    import json

    from genesis.session_awareness import pr_verification_sources as sources

    _, metadata, _, _, _, _ = source_fixture(tmp_path)
    streams = []
    requests = []

    class Stream(httpx.AsyncByteStream):
        closed = False
        read_bytes = 0

        def __init__(self, raw):
            self.raw = raw
            streams.append(self)

        async def __aiter__(self):
            for offset in range(0, len(self.raw), 10):
                if kind == "deadline":
                    await asyncio.sleep(0.01)  # Each read fits; the whole stream does not.
                self.read_bytes += len(self.raw[offset : offset + 10])
                yield self.raw[offset : offset + 10]

        async def aclose(self):
            self.closed = True

    def respond(request):
        requests.append(request)
        if kind == "redirect":
            return httpx.Response(
                302, headers={"location": "https://elsewhere.example.test"}, stream=Stream(b"")
            )
        if kind == "encoding":
            return httpx.Response(
                200,
                headers={"content-encoding": "gzip"},
                stream=Stream(json.dumps(metadata).encode()),
            )
        raw = json.dumps(metadata).encode() if len(requests) == 1 else b"large diff" * 100
        return httpx.Response(200, stream=Stream(raw))

    if kind == "metadata-cap":
        monkeypatch.setattr(sources, "MAX_METADATA", 10)
    if kind == "diff-cap":
        monkeypatch.setattr(sources, "MAX_DIFF", 10)
    if kind == "deadline":
        monkeypatch.setattr(sources, "ACQUISITION_SECONDS", 0.05)
    with pytest.raises(SourceError):
        snapshot = sources.acquire_sources(
            "owner/repo", 7, run=lambda *a, **kw: b"fixture", transport=httpx.MockTransport(respond)
        )
        sources.save_packet(snapshot, tmp_path / "never-ready")
    assert not (tmp_path / "never-ready").exists()
    assert streams and all(stream.closed for stream in streams)
    assert len(requests) == (2 if kind == "diff-cap" else 1)
    if kind in ("redirect", "encoding"):
        assert streams[0].read_bytes == 0
    else:
        assert streams[-1].read_bytes < len(streams[-1].raw)


def test_credential_diagnostics_are_disabled_and_stderr_is_not_exposed(monkeypatch):
    import sys

    from genesis.session_awareness.pr_verification_sources import _run

    monkeypatch.setenv("GH_DEBUG", "api")
    assert (
        _run(
            [sys.executable, "-c", "import os;print(os.environ.get('GH_DEBUG', 'unset'))"],
            timeout=5,
            limit=64,
        )
        == b"unset\n"
    )
    with pytest.raises(SourceError) as error:
        _run(
            [
                sys.executable,
                "-c",
                "import sys;sys.stderr.write('private-fixture-token');sys.exit(1)",
            ],
            timeout=5,
            limit=64,
        )
    assert "private-fixture-token" not in str(error.value)


@pytest.mark.parametrize("raw", [b"\xff", '{"body":"bad\\ud800"}'.encode("utf-16")])
def test_json_rejects_non_utf8(raw):
    from genesis.session_awareness.pr_verification_sources import _json

    with pytest.raises(SourceError, match="UTF-8"):
        _json(raw)


def committed(tmp_path, files):
    """A repo whose one commit holds *files* (name -> bytes, or a symlink target)."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name, value in files.items():
        if isinstance(value, tuple):
            (tmp_path / name).symlink_to(value[0])
        else:
            (tmp_path / name).write_bytes(value)
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.name=F", "-c", "user.email=f@example.test"]
        + ["-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false"]
        + ["commit", "-qm", "base"],
        check=True,
    )


def blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def test_binary_change_cut_after_its_index_line_is_refused(tmp_path):
    # Git counts such a block 0/0, the same as GitHub's census for a binary file.
    committed(tmp_path, {"b.dat": b"\x00old\xff"})
    (tmp_path / "b.dat").write_bytes(b"\x00new\xff")
    raw = subprocess.check_output(["git", "-C", str(tmp_path), "diff"])
    files = [
        dict(
            filename="b.dat", additions=0, deletions=0, status="modified", sha=blob(b"\x00new\xff")
        )
    ]
    reconcile_diff(raw, files, timeout=5)
    cut = b"\n".join(raw.split(b"\n")[:2]) + b"\n"
    with pytest.raises(SourceError, match="no hunk or binary marker"):
        reconcile_diff(cut, files, timeout=5)


def test_symlink_replaced_by_a_file_is_one_census_entry(tmp_path):
    committed(tmp_path, {"link": ("target",)})
    (tmp_path / "link").unlink()
    (tmp_path / "link").write_bytes(b"text\n")
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True)
    raw = subprocess.check_output(["git", "-C", str(tmp_path), "diff", "--cached"])
    assert raw.count(b"diff --git a/link b/link") == 2
    files = [
        dict(filename="link", additions=1, deletions=1, status="modified", sha=blob(b"text\n"))
    ]
    reconcile_diff(raw, files, timeout=5)


def rename_diff(tmp_path):
    committed(tmp_path, {"legacy-a": b"same\n"})
    subprocess.run(["git", "-C", str(tmp_path), "mv", "legacy-a", "current"], check=True)
    return subprocess.check_output(["git", "-C", str(tmp_path), "diff", "--cached", "-M"])


@pytest.mark.parametrize(
    "entry, accepted",
    [
        (dict(status="renamed", previous_filename="legacy-a"), True),
        (dict(status="renamed", previous_filename="legacy-b"), False),
        (dict(status="added"), False),
    ],
)
def test_rename_source_must_match_the_census(tmp_path, entry, accepted):
    raw = rename_diff(tmp_path)
    files = [dict(filename="current", additions=0, deletions=0, sha=blob(b"same\n"), **entry)]
    if accepted:
        reconcile_diff(raw, files, timeout=5)
    else:
        with pytest.raises(SourceError, match="rename|change type"):
            reconcile_diff(raw, files, timeout=5)


@pytest.mark.parametrize("status", ["modified", "removed"])
def test_census_blob_must_match_the_diff_index_line(tmp_path, status):
    # Measured on six merged PRs: the census sha is the post-image blob, and the
    # pre-image for a removed file.
    committed(tmp_path, {"f.txt": b"before\n"})
    if status == "removed":
        (tmp_path / "f.txt").unlink()
        counts, right = (0, 1), blob(b"before\n")
    else:
        (tmp_path / "f.txt").write_bytes(b"after\n")
        counts, right = (1, 1), blob(b"after\n")
    raw = subprocess.check_output(["git", "-C", str(tmp_path), "diff"])
    entry = dict(filename="f.txt", additions=counts[0], deletions=counts[1], status=status)
    reconcile_diff(raw, [entry | {"sha": right}], timeout=5)
    with pytest.raises(SourceError, match="census blob"):
        reconcile_diff(raw, [entry | {"sha": "f" * 40}], timeout=5)


def test_nonempty_response_that_is_not_a_patch_is_refused():
    reconcile_diff(b"", [], timeout=5)
    with pytest.raises(SourceError, match="not a recognised patch"):
        reconcile_diff(b"<html>an error page</html>\n", [], timeout=5)


def test_json_refuses_non_json_constants():
    from genesis.session_awareness.pr_verification_sources import _json

    assert _json(b'{"extra": 1}') == {"extra": 1}
    for constant in (b"NaN", b"Infinity", b"-Infinity"):
        with pytest.raises(SourceError):
            _json(b'{"extra": ' + constant + b"}")


def test_unencodable_status_is_refused_before_any_destination(tmp_path):
    from genesis.session_awareness.pr_verification_sources import _file

    entry = dict(filename="f", status="modified", sha="a" * 40, additions=0, deletions=0)
    assert _file(entry)["status"] == "modified"
    with pytest.raises(SourceError):
        _file(entry | {"status": "\ud800"})


def test_mode_pair_stripped_whole_from_a_content_change_is_refused(tmp_path):
    # Git leaves the mode off the index line only when a mode header (or a
    # create or delete) explains it, so its absence betrays a stripped pair.
    raw = mode_patch(tmp_path, content=True)
    assert b"old mode 100644\n" in raw and b"new mode 100755\n" in raw
    files = [
        dict(filename="ordinary.txt", additions=1, deletions=1, status="modified", sha=AFTER_BLOB)
    ]
    reconcile_diff(raw, files, timeout=5)
    stripped = raw.replace(b"old mode 100644\n", b"").replace(b"new mode 100755\n", b"")
    with pytest.raises(SourceError, match="mode"):
        reconcile_diff(stripped, files, timeout=5)


@pytest.mark.parametrize("index", [b"index ..", b"index ..zz", b"index abc..def"])
def test_malformed_index_ids_are_refused(tmp_path, index):
    raw = patch(tmp_path)
    lines = raw.split(b"\n")
    lines[1] = index + b" 100644"
    files = [
        dict(filename="ordinary.txt", additions=1, deletions=1, status="modified", sha=AFTER_BLOB)
    ]
    with pytest.raises(SourceError):
        reconcile_diff(b"\n".join(lines), files, timeout=5)


def test_whitespace_only_response_is_not_an_empty_diff():
    with pytest.raises(SourceError, match="not a recognised patch"):
        reconcile_diff(b" \n", [], timeout=5)


def test_uncovered_paths_are_reported_by_kind(tmp_path):
    # A pure rename has no index line, so its census blob cannot be compared;
    # a binary block has no line counts to compare.
    raw = rename_diff(tmp_path / "rename")
    files = [
        dict(filename="current", additions=0, deletions=0, sha=blob(b"same\n"))
        | dict(status="renamed", previous_filename="legacy-a")
    ]
    assert reconcile_diff(raw, files, timeout=5) == {"blob": ["current"], "counts": []}
    committed(tmp_path / "bin", {"b.dat": b"\x00old\xff"})
    (tmp_path / "bin" / "b.dat").write_bytes(b"\x00new\xff")
    raw = subprocess.check_output(["git", "-C", str(tmp_path / "bin"), "diff"])
    files = [
        dict(filename="b.dat", additions=0, deletions=0, status="modified")
        | dict(sha=blob(b"\x00new\xff"))
    ]
    assert reconcile_diff(raw, files, timeout=5) == {"blob": [], "counts": ["b.dat"]}
    plain = [
        dict(filename="ordinary.txt", additions=1, deletions=1, status="modified", sha=AFTER_BLOB)
    ]
    assert reconcile_diff(patch(tmp_path / "plain"), plain, timeout=5) == {
        "blob": [],
        "counts": [],
    }


def test_packet_keys_are_exactly_the_snapshot_fields_and_digests(tmp_path):
    # A reader (the completeness check) rebuilds the snapshot from these keys.
    from dataclasses import fields

    from genesis.session_awareness.pr_verification_sources import PrSourceSnapshot

    snapshot = PrSourceSnapshot(
        repo="owner/repo",
        pr=1,
        merge_commit="a" * 40,
        head="b" * 40,
        base="c" * 40,
        body="",
        diff="",
        files=[],
        acquired_at="2026-01-01T00:00:00+00:00",
    )
    digests = {"body_sha256", "diff_sha256", "files_sha256"}
    assert set(snapshot.packet()) == {f.name for f in fields(PrSourceSnapshot)} | digests


def test_unknown_file_status_is_refused():
    from genesis.session_awareness.pr_verification_sources import _file

    entry = dict(filename="f", status="modified", sha="a" * 40, additions=0, deletions=0)
    for status in ("added", "removed", "modified", "copied", "changed", "unchanged"):
        assert _file(entry | {"status": status})["status"] == status
    with pytest.raises(SourceError):
        _file(entry | {"status": "deleted"})


@pytest.mark.parametrize("kind", ["created", "deleted"])
def test_census_change_type_must_match_the_block(tmp_path, kind):
    # Measured on 115 blocks of seven merged PRs: a created block is always
    # "added" and a deleted block always "removed" in GitHub's census.
    committed(tmp_path, {"keep.txt": b"keep\n"})
    if kind == "created":
        (tmp_path / "new.txt").write_bytes(b"")
        subprocess.run(["git", "-C", str(tmp_path), "add", "new.txt"], check=True)
        raw = subprocess.check_output(["git", "-C", str(tmp_path), "diff", "--cached"])
        entry = dict(filename="new.txt", additions=0, deletions=0, sha=blob(b""))
        right, wrong = "added", "modified"
    else:
        (tmp_path / "keep.txt").unlink()
        raw = subprocess.check_output(["git", "-C", str(tmp_path), "diff"])
        entry = dict(filename="keep.txt", additions=0, deletions=1, sha=blob(b"keep\n"))
        right, wrong = "removed", "modified"
    reconcile_diff(raw, [entry | {"status": right}], timeout=5)
    with pytest.raises(SourceError, match="change type"):
        reconcile_diff(raw, [entry | {"status": wrong}], timeout=5)
