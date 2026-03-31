"""Tests for inline PreToolUse hooks defined in .claude/settings.json.

These hooks are the last line of defense against dangerous operations in
Claude Code sessions. They run as bash commands with CLAUDE_TOOL_INPUT set
to the JSON-serialized tool input.

Exit codes:
    0 = allowed (hook passes)
    2 = blocked (hook rejects the tool call)
"""

from __future__ import annotations

import pytest

from tests.test_hooks.conftest import run_hook

# ---------------------------------------------------------------------------
# Bash hook: pip install -e / --editable to worktree paths
# ---------------------------------------------------------------------------


class TestBashHookPipEditable:
    """Block pip install -e pointing to worktree directories."""

    def test_pip_install_e_worktree_blocked(self, bash_hook_command: str) -> None:
        """pip install -e ./.claude/worktrees/foo -> BLOCKED."""
        result = run_hook(
            bash_hook_command,
            {"command": "pip install -e ./.claude/worktrees/foo"},
        )
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_pip_install_editable_worktree_blocked(
        self, bash_hook_command: str
    ) -> None:
        """pip install --editable ./.claude/worktrees/foo -> BLOCKED."""
        result = run_hook(
            bash_hook_command,
            {"command": "pip install --editable ./.claude/worktrees/foo"},
        )
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr
        assert "PYTHONPATH" in result.stderr  # suggests alternative

    def test_pip_install_e_absolute_worktree_blocked(
        self, bash_hook_command: str
    ) -> None:
        """pip install -e ${HOME}/genesis/.claude/worktrees/my-branch -> BLOCKED."""
        result = run_hook(
            bash_hook_command,
            {
                "command": (
                    "pip install -e "
                    "${HOME}/genesis/.claude/worktrees/my-branch"
                )
            },
        )
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_pip_install_normal_package_allowed(
        self, bash_hook_command: str
    ) -> None:
        """pip install requests -> allowed (no worktree, no -e)."""
        result = run_hook(
            bash_hook_command, {"command": "pip install requests"}
        )
        assert result.returncode == 0

    def test_pip_install_e_non_worktree_allowed(
        self, bash_hook_command: str
    ) -> None:
        """pip install -e ./src -> allowed (not a worktree path)."""
        result = run_hook(
            bash_hook_command, {"command": "pip install -e ./src"}
        )
        assert result.returncode == 0

    def test_pip_install_e_with_extras_worktree_blocked(
        self, bash_hook_command: str
    ) -> None:
        """pip install -e '.claude/worktrees/x[dev]' -> BLOCKED."""
        result = run_hook(
            bash_hook_command,
            {"command": "pip install -e .claude/worktrees/x[dev]"},
        )
        assert result.returncode == 2


# ---------------------------------------------------------------------------
# Bash hook: git worktree remove --force / -f
# ---------------------------------------------------------------------------


class TestBashHookWorktreeForceRemove:
    """Block git worktree remove --force (destroys uncommitted work)."""

    def test_worktree_remove_force_blocked(
        self, bash_hook_command: str
    ) -> None:
        """git worktree remove --force .claude/worktrees/foo -> BLOCKED."""
        result = run_hook(
            bash_hook_command,
            {"command": "git worktree remove --force .claude/worktrees/foo"},
        )
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_worktree_remove_f_blocked(
        self, bash_hook_command: str
    ) -> None:
        """git worktree remove -f .claude/worktrees/foo -> BLOCKED."""
        result = run_hook(
            bash_hook_command,
            {"command": "git worktree remove -f .claude/worktrees/foo"},
        )
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_worktree_remove_without_force_allowed(
        self, bash_hook_command: str
    ) -> None:
        """git worktree remove .claude/worktrees/foo -> allowed (no --force)."""
        result = run_hook(
            bash_hook_command,
            {"command": "git worktree remove .claude/worktrees/foo"},
        )
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Bash hook: rm -rf on broad paths
# ---------------------------------------------------------------------------


class TestBashHookRmRf:
    """Block rm -rf on broad/dangerous paths.

    The hook uses glob patterns like *"rm -rf /"* which means ANY command
    containing the literal substring 'rm -rf /' is blocked, including
    specific absolute paths like /tmp/foo. This is intentionally aggressive.
    """

    def test_rm_rf_root_blocked(self, bash_hook_command: str) -> None:
        """rm -rf / -> BLOCKED."""
        result = run_hook(bash_hook_command, {"command": "rm -rf /"})
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_rm_rf_home_blocked(self, bash_hook_command: str) -> None:
        """rm -rf ~ -> BLOCKED."""
        result = run_hook(bash_hook_command, {"command": "rm -rf ~"})
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_rm_rf_dot_blocked(self, bash_hook_command: str) -> None:
        """rm -rf . -> BLOCKED."""
        result = run_hook(bash_hook_command, {"command": "rm -rf ."})
        assert result.returncode == 2

    def test_rm_rf_dotdot_blocked(self, bash_hook_command: str) -> None:
        """rm -rf .. -> BLOCKED."""
        result = run_hook(bash_hook_command, {"command": "rm -rf .."})
        assert result.returncode == 2

    def test_rm_rf_absolute_path_blocked(self, bash_hook_command: str) -> None:
        """rm -rf /tmp/foo -> BLOCKED (substring 'rm -rf /' matches).

        The hook pattern *"rm -rf /"* catches ALL absolute-path rm -rf.
        This is intentionally aggressive — the hook errs on the side of
        safety. Use 'rm -r' (no -f) or ask the user for specific paths.
        """
        result = run_hook(bash_hook_command, {"command": "rm -rf /tmp/foo"})
        assert result.returncode == 2

    def test_rm_rf_relative_subpath_blocked(
        self, bash_hook_command: str
    ) -> None:
        """rm -rf ./some/specific/deep/path -> BLOCKED.

        The pattern *"rm -rf ."* matches because the command contains
        the substring 'rm -rf .'. All ./relative rm -rf is caught.
        """
        result = run_hook(
            bash_hook_command,
            {"command": "rm -rf ./some/specific/deep/path"},
        )
        assert result.returncode == 2

    def test_rm_rf_home_subpath_blocked(self, bash_hook_command: str) -> None:
        """rm -rf ~/Downloads -> BLOCKED (substring 'rm -rf ~' matches)."""
        result = run_hook(
            bash_hook_command, {"command": "rm -rf ~/Downloads"}
        )
        assert result.returncode == 2

    def test_rm_rf_bare_dirname_allowed(self, bash_hook_command: str) -> None:
        """rm -rf somedir -> allowed.

        Bare directory names (no leading /, ~, or .) are not caught by
        the hook patterns. This is the escape hatch for specific cleanup.
        """
        result = run_hook(bash_hook_command, {"command": "rm -rf somedir"})
        assert result.returncode == 0

    def test_rm_r_no_force_allowed(self, bash_hook_command: str) -> None:
        """rm -r / -> allowed (no -f flag, pattern requires 'rm -rf')."""
        result = run_hook(bash_hook_command, {"command": "rm -r /"})
        assert result.returncode == 0

    def test_rm_single_file_allowed(self, bash_hook_command: str) -> None:
        """rm foo.txt -> allowed (no -rf)."""
        result = run_hook(bash_hook_command, {"command": "rm foo.txt"})
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Bash hook: git push --force / -f
# ---------------------------------------------------------------------------


class TestBashHookGitPushForce:
    """Block force pushes."""

    def test_git_push_force_blocked(self, bash_hook_command: str) -> None:
        """git push --force origin main -> BLOCKED."""
        result = run_hook(
            bash_hook_command,
            {"command": "git push --force origin main"},
        )
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr
        assert "Force push" in result.stderr

    def test_git_push_f_blocked(self, bash_hook_command: str) -> None:
        """git push -f -> BLOCKED."""
        result = run_hook(bash_hook_command, {"command": "git push -f"})
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_git_push_f_with_remote_blocked(
        self, bash_hook_command: str
    ) -> None:
        """git push -f origin feature -> BLOCKED."""
        result = run_hook(
            bash_hook_command,
            {"command": "git push -f origin feature"},
        )
        assert result.returncode == 2

    def test_git_push_u_then_f_blocked(self, bash_hook_command: str) -> None:
        """git push -u origin -f main -> BLOCKED (-f anywhere after 'git push')."""
        result = run_hook(
            bash_hook_command,
            {"command": "git push -u origin -f main"},
        )
        assert result.returncode == 2

    def test_git_push_force_with_lease_blocked(
        self, bash_hook_command: str
    ) -> None:
        """git push --force-with-lease -> BLOCKED.

        The pattern *"--force"* matches --force-with-lease too. This is
        intentional — even safe-ish force pushes require explicit user approval.
        """
        result = run_hook(
            bash_hook_command,
            {"command": "git push --force-with-lease origin main"},
        )
        assert result.returncode == 2

    def test_git_push_normal_allowed(self, bash_hook_command: str) -> None:
        """git push origin feature-branch -> allowed (no force)."""
        result = run_hook(
            bash_hook_command,
            {"command": "git push origin feature-branch"},
        )
        assert result.returncode == 0

    def test_git_push_u_allowed(self, bash_hook_command: str) -> None:
        """git push -u origin feature -> allowed (-u is not -f)."""
        result = run_hook(
            bash_hook_command,
            {"command": "git push -u origin feature"},
        )
        assert result.returncode == 0

    def test_git_push_no_args_allowed(self, bash_hook_command: str) -> None:
        """git push -> allowed."""
        result = run_hook(bash_hook_command, {"command": "git push"})
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Bash hook: git reset --hard
# ---------------------------------------------------------------------------


class TestBashHookGitResetHard:
    """Block git reset --hard."""

    def test_git_reset_hard_blocked(self, bash_hook_command: str) -> None:
        """git reset --hard -> BLOCKED."""
        result = run_hook(
            bash_hook_command, {"command": "git reset --hard"}
        )
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr
        assert "git stash" in result.stderr  # suggests alternative

    def test_git_reset_hard_with_ref_blocked(
        self, bash_hook_command: str
    ) -> None:
        """git reset --hard HEAD~3 -> BLOCKED."""
        result = run_hook(
            bash_hook_command, {"command": "git reset --hard HEAD~3"}
        )
        assert result.returncode == 2

    def test_git_reset_hard_origin_blocked(
        self, bash_hook_command: str
    ) -> None:
        """git reset --hard origin/main -> BLOCKED."""
        result = run_hook(
            bash_hook_command,
            {"command": "git reset --hard origin/main"},
        )
        assert result.returncode == 2

    def test_git_reset_soft_allowed(self, bash_hook_command: str) -> None:
        """git reset --soft HEAD~1 -> allowed."""
        result = run_hook(
            bash_hook_command, {"command": "git reset --soft HEAD~1"}
        )
        assert result.returncode == 0

    def test_git_reset_mixed_allowed(self, bash_hook_command: str) -> None:
        """git reset HEAD~1 -> allowed (default mixed mode)."""
        result = run_hook(
            bash_hook_command, {"command": "git reset HEAD~1"}
        )
        assert result.returncode == 0

    def test_git_reset_no_args_allowed(self, bash_hook_command: str) -> None:
        """git reset -> allowed (unstages all)."""
        result = run_hook(bash_hook_command, {"command": "git reset"})
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Bash hook: git clean -f / -fd
# ---------------------------------------------------------------------------


class TestBashHookGitClean:
    """Block git clean with force flags."""

    def test_git_clean_f_blocked(self, bash_hook_command: str) -> None:
        """git clean -f -> BLOCKED."""
        result = run_hook(bash_hook_command, {"command": "git clean -f"})
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_git_clean_fd_blocked(self, bash_hook_command: str) -> None:
        """git clean -fd -> BLOCKED."""
        result = run_hook(bash_hook_command, {"command": "git clean -fd"})
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_git_clean_fdx_blocked(self, bash_hook_command: str) -> None:
        """git clean -fdx -> BLOCKED (contains 'git clean -fd')."""
        result = run_hook(bash_hook_command, {"command": "git clean -fdx"})
        assert result.returncode == 2

    def test_git_clean_fx_blocked(self, bash_hook_command: str) -> None:
        """git clean -fx -> BLOCKED (contains 'git clean -f')."""
        result = run_hook(bash_hook_command, {"command": "git clean -fx"})
        assert result.returncode == 2

    def test_git_clean_n_allowed(self, bash_hook_command: str) -> None:
        """git clean -n -> allowed (dry run, no -f)."""
        result = run_hook(bash_hook_command, {"command": "git clean -n"})
        assert result.returncode == 0

    def test_git_clean_nd_allowed(self, bash_hook_command: str) -> None:
        """git clean -nd -> allowed (dry run with directories)."""
        result = run_hook(bash_hook_command, {"command": "git clean -nd"})
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Bash hook: benign commands (should all pass)
# ---------------------------------------------------------------------------


class TestBashHookBenignCommands:
    """Normal commands must not be blocked."""

    @pytest.mark.parametrize(
        "cmd",
        [
            "ls -la",
            "python -m pytest",
            "git status",
            "git diff --cached",
            "git log --oneline -10",
            "git add src/genesis/foo.py",
            "git commit -m 'fix: something'",
            "cat /etc/hostname",
            "ruff check .",
            "pip install requests httpx",
            "pip install -r requirements.txt",
            "source ~/genesis/.venv/bin/activate",
            "curl -s http://localhost:6333/collections",
            "echo hello world",
            "cd ${HOME}/genesis && pytest -v",
            "PYTHONPATH=.claude/worktrees/foo/src pytest tests/",
        ],
        ids=[
            "ls",
            "pytest",
            "git-status",
            "git-diff",
            "git-log",
            "git-add",
            "git-commit",
            "cat",
            "ruff",
            "pip-install-packages",
            "pip-install-requirements",
            "source-venv",
            "curl",
            "echo",
            "cd-and-pytest",
            "pythonpath-worktree",
        ],
    )
    def test_benign_command_allowed(
        self, bash_hook_command: str, cmd: str
    ) -> None:
        """Normal commands pass through the hook."""
        result = run_hook(bash_hook_command, {"command": cmd})
        assert result.returncode == 0, (
            f"Benign command was blocked: {cmd!r}\nstderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# Bash hook: error message quality
# ---------------------------------------------------------------------------


class TestBashHookErrorMessages:
    """Verify hook stderr contains actionable guidance."""

    def test_pip_editable_suggests_pythonpath(
        self, bash_hook_command: str
    ) -> None:
        result = run_hook(
            bash_hook_command,
            {"command": "pip install -e .claude/worktrees/branch"},
        )
        assert result.returncode == 2
        assert "PYTHONPATH" in result.stderr
        assert "worktree" in result.stderr.lower()

    def test_force_push_suggests_pr(self, bash_hook_command: str) -> None:
        result = run_hook(
            bash_hook_command,
            {"command": "git push --force origin main"},
        )
        assert result.returncode == 2
        assert "PR" in result.stderr

    def test_reset_hard_suggests_stash(self, bash_hook_command: str) -> None:
        result = run_hook(
            bash_hook_command, {"command": "git reset --hard"}
        )
        assert result.returncode == 2
        assert "stash" in result.stderr

    def test_git_clean_suggests_user(self, bash_hook_command: str) -> None:
        result = run_hook(bash_hook_command, {"command": "git clean -f"})
        assert result.returncode == 2
        assert "user" in result.stderr.lower()

    def test_rm_rf_suggests_specific(self, bash_hook_command: str) -> None:
        result = run_hook(bash_hook_command, {"command": "rm -rf /"})
        assert result.returncode == 2
        assert "specific" in result.stderr.lower() or "user" in result.stderr.lower()


# ---------------------------------------------------------------------------
# Bash hook: edge cases and combined commands
# ---------------------------------------------------------------------------


class TestBashHookEdgeCases:
    """Edge cases for the Bash hook."""

    def test_empty_command_allowed(self, bash_hook_command: str) -> None:
        """Empty command -> allowed."""
        result = run_hook(bash_hook_command, {"command": ""})
        assert result.returncode == 0

    def test_multiline_command_with_blocked(
        self, bash_hook_command: str
    ) -> None:
        """Multiline command containing rm -rf / -> BLOCKED."""
        result = run_hook(
            bash_hook_command,
            {"command": "echo hello\nrm -rf /\necho done"},
        )
        assert result.returncode == 2

    def test_chained_command_with_blocked(
        self, bash_hook_command: str
    ) -> None:
        """Command chained with && containing blocked op -> BLOCKED."""
        result = run_hook(
            bash_hook_command,
            {"command": "ls -la && git push --force origin main"},
        )
        assert result.returncode == 2

    def test_piped_command_with_blocked(
        self, bash_hook_command: str
    ) -> None:
        """Piped command containing blocked op -> BLOCKED."""
        result = run_hook(
            bash_hook_command,
            {"command": "echo yes | git push --force origin main"},
        )
        assert result.returncode == 2

    def test_subshell_with_blocked(self, bash_hook_command: str) -> None:
        """Subshell containing blocked op -> BLOCKED."""
        result = run_hook(
            bash_hook_command,
            {"command": "$(git reset --hard)"},
        )
        assert result.returncode == 2

    def test_malformed_json_input(self, bash_hook_command: str) -> None:
        """Malformed JSON in CLAUDE_TOOL_INPUT -> graceful (jq fails, no crash).

        When jq can't parse the input, CMD becomes empty string, which
        doesn't match any blocked pattern, so the hook passes.
        """
        import os
        import subprocess

        env = {**os.environ, "CLAUDE_TOOL_INPUT": "not-json{{{"}
        result = subprocess.run(
            bash_hook_command,
            shell=True,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        # Should not crash — either exit 0 (pass) or handle gracefully
        assert result.returncode in (0, 2)

    def test_missing_command_field(self, bash_hook_command: str) -> None:
        """JSON without 'command' field -> jq returns null, hook passes."""
        result = run_hook(bash_hook_command, {"url": "https://example.com"})
        assert result.returncode == 0

    def test_no_tool_input_env(self, bash_hook_command: str) -> None:
        """No CLAUDE_TOOL_INPUT env var set -> hook handles gracefully."""
        import os
        import subprocess

        env = {k: v for k, v in os.environ.items() if k != "CLAUDE_TOOL_INPUT"}
        result = subprocess.run(
            bash_hook_command,
            shell=True,
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        # Should not crash
        assert result.returncode in (0, 2)


# ---------------------------------------------------------------------------
# WebFetch hook: YouTube URL blocking
# ---------------------------------------------------------------------------


class TestWebFetchHookYouTubeBlocking:
    """Block YouTube URLs in WebFetch."""

    def test_youtube_watch_blocked(self, webfetch_hook_command: str) -> None:
        """https://www.youtube.com/watch?v=abc -> BLOCKED."""
        result = run_hook(
            webfetch_hook_command,
            {"url": "https://www.youtube.com/watch?v=abc"},
        )
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr
        assert "YouTube" in result.stderr

    def test_youtube_short_url_blocked(
        self, webfetch_hook_command: str
    ) -> None:
        """https://youtu.be/abc123 -> BLOCKED."""
        result = run_hook(
            webfetch_hook_command, {"url": "https://youtu.be/abc123"}
        )
        assert result.returncode == 2
        assert "BLOCKED" in result.stderr

    def test_youtube_no_www_blocked(
        self, webfetch_hook_command: str
    ) -> None:
        """https://youtube.com/watch?v=xyz -> BLOCKED."""
        result = run_hook(
            webfetch_hook_command,
            {"url": "https://youtube.com/watch?v=xyz"},
        )
        assert result.returncode == 2

    def test_youtube_uppercase_blocked(
        self, webfetch_hook_command: str
    ) -> None:
        """https://www.YOUTUBE.COM/watch?v=abc -> BLOCKED (case-insensitive)."""
        result = run_hook(
            webfetch_hook_command,
            {"url": "https://www.YOUTUBE.COM/watch?v=abc"},
        )
        assert result.returncode == 2

    def test_youtube_mixed_case_blocked(
        self, webfetch_hook_command: str
    ) -> None:
        """https://YouTube.com/playlist?list=PL... -> BLOCKED."""
        result = run_hook(
            webfetch_hook_command,
            {"url": "https://YouTube.com/playlist?list=PLabc"},
        )
        assert result.returncode == 2

    def test_youtube_embed_blocked(
        self, webfetch_hook_command: str
    ) -> None:
        """https://www.youtube.com/embed/abc -> BLOCKED."""
        result = run_hook(
            webfetch_hook_command,
            {"url": "https://www.youtube.com/embed/abc123"},
        )
        assert result.returncode == 2

    def test_youtu_be_mixed_case_blocked(
        self, webfetch_hook_command: str
    ) -> None:
        """https://YOUTU.BE/abc -> BLOCKED."""
        result = run_hook(
            webfetch_hook_command, {"url": "https://YOUTU.BE/abc123"}
        )
        assert result.returncode == 2


# ---------------------------------------------------------------------------
# WebFetch hook: allowed URLs
# ---------------------------------------------------------------------------


class TestWebFetchHookAllowedUrls:
    """Non-YouTube URLs must pass through."""

    @pytest.mark.parametrize(
        "url",
        [
            "https://example.com",
            "https://github.com/YOUR_GITHUB_USER/Genesis",
            "https://docs.python.org/3/library/asyncio.html",
            "https://api.openai.com/v1/models",
            "https://www.google.com/search?q=python",
            "https://stackoverflow.com/questions/12345",
            "https://vimeo.com/12345",  # video site, but not YouTube
            "https://dailymotion.com/video/abc",
            "https://httpbin.org/get",
        ],
        ids=[
            "example",
            "github",
            "python-docs",
            "openai-api",
            "google",
            "stackoverflow",
            "vimeo",
            "dailymotion",
            "httpbin",
        ],
    )
    def test_non_youtube_allowed(
        self, webfetch_hook_command: str, url: str
    ) -> None:
        """Non-YouTube URLs pass through the hook."""
        result = run_hook(webfetch_hook_command, {"url": url})
        assert result.returncode == 0, (
            f"Non-YouTube URL was blocked: {url!r}\nstderr: {result.stderr}"
        )


# ---------------------------------------------------------------------------
# WebFetch hook: error message quality
# ---------------------------------------------------------------------------


class TestWebFetchHookErrorMessages:
    """Verify YouTube block message contains actionable guidance."""

    def test_suggests_yt_dlp(self, webfetch_hook_command: str) -> None:
        """Error message suggests yt-dlp as alternative."""
        result = run_hook(
            webfetch_hook_command,
            {"url": "https://www.youtube.com/watch?v=test"},
        )
        assert result.returncode == 2
        assert "yt-dlp" in result.stderr

    def test_mentions_ssl(self, webfetch_hook_command: str) -> None:
        """Error message explains the SSL root cause."""
        result = run_hook(
            webfetch_hook_command,
            {"url": "https://www.youtube.com/watch?v=test"},
        )
        assert "SSL" in result.stderr

    def test_shows_transcript_example(
        self, webfetch_hook_command: str
    ) -> None:
        """Error message includes transcript extraction example."""
        result = run_hook(
            webfetch_hook_command,
            {"url": "https://www.youtube.com/watch?v=test"},
        )
        assert "write-auto-sub" in result.stderr or "transcript" in result.stderr.lower()


# ---------------------------------------------------------------------------
# WebFetch hook: edge cases
# ---------------------------------------------------------------------------


class TestWebFetchHookEdgeCases:
    """Edge cases for the WebFetch hook."""

    def test_empty_url_allowed(self, webfetch_hook_command: str) -> None:
        """Empty URL -> allowed (no match)."""
        result = run_hook(webfetch_hook_command, {"url": ""})
        assert result.returncode == 0

    def test_missing_url_field(self, webfetch_hook_command: str) -> None:
        """JSON without 'url' field -> jq returns null, hook passes."""
        result = run_hook(webfetch_hook_command, {"command": "ls"})
        assert result.returncode == 0

    def test_youtube_in_query_param_blocked(
        self, webfetch_hook_command: str
    ) -> None:
        """URL with youtube.com in the domain -> BLOCKED even with params."""
        result = run_hook(
            webfetch_hook_command,
            {"url": "https://www.youtube.com/results?search_query=python"},
        )
        assert result.returncode == 2

    def test_url_containing_youtube_as_substring_blocked(
        self, webfetch_hook_command: str
    ) -> None:
        """notyoutube.com contains 'youtube.com' substring -> BLOCKED.

        The grep pattern matches any URL containing the substring
        'youtube.com', including domains like notyoutube.com. This is
        a known acceptable false positive — it's better to over-block
        than to miss actual YouTube URLs.
        """
        result = run_hook(
            webfetch_hook_command,
            {"url": "https://notyoutube.com/video"},
        )
        # This IS blocked because grep matches the substring
        assert result.returncode == 2


# ---------------------------------------------------------------------------
# Settings.json structure validation
# ---------------------------------------------------------------------------


class TestSettingsStructure:
    """Validate that .claude/settings.json has the expected hook structure."""

    def test_has_pretooluse_hooks(self, settings: dict) -> None:
        """settings.json contains PreToolUse hooks section."""
        assert "hooks" in settings
        assert "PreToolUse" in settings["hooks"]
        assert isinstance(settings["hooks"]["PreToolUse"], list)

    def test_has_bash_matcher(self, settings: dict) -> None:
        """PreToolUse section has a Bash matcher entry."""
        matchers = [
            h.get("matcher") for h in settings["hooks"]["PreToolUse"]
        ]
        assert "Bash" in matchers

    def test_has_webfetch_matcher(self, settings: dict) -> None:
        """PreToolUse section has a WebFetch matcher entry."""
        matchers = [
            h.get("matcher") for h in settings["hooks"]["PreToolUse"]
        ]
        assert "WebFetch" in matchers

    def test_bash_hook_is_command(self, settings: dict) -> None:
        """Bash hooks are type=command (inline bash -c or Python script)."""
        for entry in settings["hooks"]["PreToolUse"]:
            if entry.get("matcher") == "Bash":
                hooks = entry["hooks"]
                commands = [
                    h
                    for h in hooks
                    if h.get("type") == "command"
                    and h.get("command", "").strip()
                ]
                assert len(commands) >= 1, "No command hook found for Bash matcher"

    def test_webfetch_hook_is_inline_command(self, settings: dict) -> None:
        """WebFetch hook is type=command and starts with 'bash -c'."""
        for entry in settings["hooks"]["PreToolUse"]:
            if entry.get("matcher") == "WebFetch":
                hooks = entry["hooks"]
                inline = [
                    h
                    for h in hooks
                    if h.get("type") == "command"
                    and h.get("command", "").startswith("bash -c")
                ]
                assert len(inline) >= 1, "No inline WebFetch hook found"

    def test_bash_hook_checks_all_expected_patterns(
        self, bash_hook_command: str
    ) -> None:
        """Bash hook script contains checks for all expected danger patterns."""
        from pathlib import Path

        # The command may be an external script path or inline bash -c.
        # Read the script file to check patterns.
        if bash_hook_command.startswith("bash -c"):
            content = bash_hook_command
        else:
            content = Path(bash_hook_command).read_text()
        assert "pip install" in content
        assert "worktree" in content
        assert "rm -rf" in content
        assert "git push" in content
        assert "--force" in content
        assert "git reset --hard" in content
        assert "git clean" in content

    def test_webfetch_hook_checks_youtube(
        self, webfetch_hook_command: str
    ) -> None:
        """WebFetch hook command contains YouTube pattern check."""
        assert "youtube" in webfetch_hook_command.lower()
        assert "youtu.be" in webfetch_hook_command.lower() or "youtu\\.be" in webfetch_hook_command
