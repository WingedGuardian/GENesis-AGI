"""Tool-runner carriers reveal the command they wrap — `uv run pytest`, `uvx pytest`.

The full-suite guard keys on ``seg.exe == "pytest"``. Before this change, a
pytest run fronted by a package-manager runner (``uv run pytest tests/``,
``poetry run pytest tests/``, ``uvx pytest tests/``) resolved its exe to the
front-end, so the guard never fired — MEASURED as rc=0 where the bare form
rc=2'd, for uv/uvx/poetry/hatch/pdm/xvfb-run alike.

The safety property under test alongside the reveal: the ``run`` family is
gated on the literal ``run`` subcommand, NOT modelled as a positional-consuming
wrapper. A blanket wrapper entry would consume the first bare word of EVERY
subcommand — ``uv rm -rf /`` would eat ``rm`` and resolve past it, HIDING a
command the destructive gate catches today. So ``uv <anything-but-run>`` must
keep resolving to ``uv`` itself.

Corpus replay (this session's own probe commands excluded): 37,568 real Bash
commands, 1 resolution diff — ``poetry run python -c …`` now revealing
``python`` — which is the fix behaving, not a regression.
"""

import importlib.util
import sys
from pathlib import Path

_HOOKS = Path(__file__).resolve().parents[2] / "scripts" / "hooks"


def _load():
    spec = importlib.util.spec_from_file_location("shell_parse_rc", _HOOKS / "shell_parse.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["shell_parse_rc"] = mod
    spec.loader.exec_module(mod)
    return mod


sp = _load()


def exes(cmd: str) -> list[str]:
    return [s.exe for s in sp.analyze(cmd)]


class TestRunCarrierReveals:
    """`<front-end> run <cmd>` resolves to <cmd> — the guard-visible direction."""

    def test_uv_run_pytest_reveals_pytest(self):
        assert exes("uv run pytest tests/") == ["pytest"]

    def test_poetry_run_pytest_reveals_pytest(self):
        assert exes("poetry run pytest tests/") == ["pytest"]

    def test_hatch_pdm_pipenv_rye_run(self):
        for fe in ("hatch", "pdm", "pipenv", "rye"):
            assert exes(f"{fe} run pytest tests/") == ["pytest"], fe

    def test_hatch_env_qualified_command_reveals_command(self):
        # `hatch run [ENV:]COMMAND` — the environment selector is NOT part of
        # the command name. Hatch treats the FIRST colon as the selector, and
        # the command may retain later colons.
        assert exes("hatch run test:pytest tests/") == ["pytest"]
        assert exes("hatch run py310,py311:pytest tests/") == ["pytest"]
        assert exes("hatch run lint:all") == ["all"]
        assert exes("hatch run +py=3.12 test:pytest tests/") == ["pytest"]
        assert exes("hatch run test:command:with-colon") == ["command:with-colon"]

    def test_colon_token_only_unwrapped_for_hatch(self):
        # The ENV: split is Hatch's documented form, not a general rewrite:
        # another carrier's `a:b` token is a command name verbatim.
        assert exes("uv run a:b") == ["a:b"]

    def test_uv_run_with_value_flags_before_command(self):
        # value-flags on `run` are consumed; the wrapped command is still found
        assert exes("uv run --python 3.12 pytest tests/") == ["pytest"]
        assert exes("uv run --with requests pytest tests/") == ["pytest"]

    def test_uv_run_double_dash_then_command(self):
        assert exes("uv run -- pytest tests/") == ["pytest"]

    def test_hatch_run_double_dash_then_env_qualified_command(self):
        assert exes("hatch run -- test:pytest tests/") == ["pytest"]

    def test_run_carrier_stacks_with_ordinary_wrappers(self):
        # timeout is an existing _WRAPPER_SPEC carrier; they compose
        assert exes("timeout 600 uv run pytest tests/") == ["pytest"]


class TestDirectCarriers:
    """uvx / xvfb-run take the wrapped command directly (no `run` literal)."""

    def test_uvx_reveals_wrapped_command(self):
        assert exes("uvx pytest tests/") == ["pytest"]

    def test_uvx_value_flag_consumed(self):
        assert exes("uvx --from pytest-cov pytest tests/") == ["pytest"]

    def test_xvfb_run_reveals_wrapped_command(self):
        assert exes("xvfb-run pytest tests/") == ["pytest"]


class TestNonRunSubcommandsStayOpaque:
    """The safety direction: only the `run` literal carries. Anything else must
    resolve to the front-end itself, so no token is ever skipped PAST."""

    def test_uv_pip_is_uv(self):
        assert exes("uv pip install pytest") == ["uv"]

    def test_uv_rm_is_uv_not_rm_skip(self):
        # A blanket wrapper would consume `rm` and resolve to the path operand.
        # `uv rm` must resolve to `uv` — never past the rm.
        assert exes("uv rm -rf /somewhere") == ["uv"]

    def test_poetry_install_is_poetry(self):
        assert exes("poetry install") == ["poetry"]

    def test_uv_run_with_no_command_is_uv(self):
        # `uv run --flag` and bare `uv run` wrap nothing; front-end stays the exe
        assert exes("uv run") == ["uv"]

    def test_run_as_flag_value_is_not_the_subcommand(self):
        # `--python run` consumes `run` as a VALUE; pytest here is uv's own
        # first bare word (a subcommand position), not a wrapped command.
        assert exes("uv --directory run pip install x") == ["uv"]


class TestGuardIntegration:
    """The consumer this exists for: full_suite_guard blocks through carriers."""

    def _guard_rc(self, cmd: str) -> int:
        import json
        import subprocess

        payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}})
        proc = subprocess.run(
            [sys.executable, str(_HOOKS / "full_suite_guard.py")],
            input=payload,
            capture_output=True,
            text=True,
        )
        return proc.returncode

    def test_uv_run_bare_directory_blocked(self):
        assert self._guard_rc("uv run pytest tests/") == 2

    def test_uvx_bare_directory_blocked(self):
        assert self._guard_rc("uvx pytest tests/") == 2

    def test_uv_run_targeted_file_allowed(self):
        assert self._guard_rc("uv run pytest tests/test_x.py") == 0

    def test_uv_run_selector_allowed(self):
        assert self._guard_rc("uv run pytest tests/ -k foo") == 0

    def test_hatch_env_qualified_bare_directory_blocked(self):
        assert self._guard_rc("hatch run test:pytest tests/") == 2

    def test_plain_forms_unchanged(self):
        assert self._guard_rc("pytest tests/") == 2
        assert self._guard_rc("pytest tests/test_x.py") == 0


class TestUnlistedCarrierOptionDoesNotHideTheCommand:
    """An option the wrapper table does not list must not become the answer.

    The table of uv's value-taking options is an OPEN set — four missing entries
    were reported on this PR alone. The failure it produced was fail-OPEN, not a
    short list: `uvx --directory /tmp pytest` resolved its exe to ``tmp``, so the
    guard concluded "not pytest" and allowed a whole-suite run that `uvx pytest`
    blocks. The fix does not extend the table. It stops the walk at the first
    option of unknown arity and leaves the segment ON the carrier, which is the
    one state full_suite_guard can still recover a command from.
    """

    def test_an_unlisted_option_leaves_the_segment_on_the_carrier(self):
        assert exes("uvx --directory /tmp pytest tests/") == ["uvx"]

    def test_the_option_value_never_becomes_the_executable(self):
        # The pre-fix reading, and the one that made the guard say "not pytest".
        assert "tmp" not in exes("uvx --directory /tmp pytest tests/")

    def test_a_listed_option_still_reveals_the_command(self):
        # Guard the guard: stopping early must not blunt the reveal this PR is for.
        assert exes("uvx --with foo pytest tests/") == ["pytest"]
        assert exes("uvx pytest tests/") == ["pytest"]

    def test_an_inline_value_is_not_ambiguous_and_still_reveals(self):
        assert exes("uvx --directory=/tmp pytest tests/") == ["pytest"]

    def test_the_run_family_is_untouched_by_this(self):
        # `uv run` resolves past an unknown flag as before: leaving THAT opaque
        # would hide a wrapped command from every gate keying on seg.exe, which
        # is the direction the resolver promises never to take.
        assert exes("uv run --unknown-flag pytest tests/") == ["pytest"]


class TestHatchSelectorSequence:
    """Hatch's matrix selectors come in both signs and may end with ``--``."""

    def test_a_terminator_after_a_selector_is_not_the_command(self):
        assert exes("hatch run +py=3.12 -- test:pytest tests/") == ["pytest"]

    def test_an_excluding_selector_is_walked_past(self):
        assert exes("hatch run +py=3.12 -py=3.9 test:pytest tests/") == ["pytest"]

    def test_a_bare_env_qualified_command_is_unchanged(self):
        assert exes("hatch run test:pytest tests/") == ["pytest"]


class TestCarrierOptionGuardIntegration(TestGuardIntegration):
    """The same shapes as verdicts, through the real guard."""

    def test_unlisted_uvx_options_block_a_whole_suite_run(self):
        assert self._guard_rc("uvx --directory /tmp pytest") == 2
        assert self._guard_rc("uvx --env-file .env pytest") == 2
        assert self._guard_rc("uvx --with-requirements reqs.txt pytest") == 2

    def test_hatch_selector_sequences_block_a_whole_suite_run(self):
        assert self._guard_rc("hatch run +py=3.12 -- test:pytest tests/") == 2
        assert self._guard_rc("hatch run +py=3.12 -py=3.9 test:pytest tests/") == 2

    def test_a_targeted_run_behind_an_unlisted_option_is_still_allowed(self):
        assert self._guard_rc("uvx --directory /tmp pytest tests/test_x.py") == 0

    def test_a_non_pytest_command_behind_an_unlisted_option_is_allowed(self):
        # The over-block this fix must NOT cause: losing confidence in which
        # token is the command may add a token read, never a blanket refusal.
        assert self._guard_rc("uvx --directory /tmp ruff check .") == 0

    def test_pytest_named_as_a_dependency_is_still_not_a_run(self):
        assert self._guard_rc("uvx --with pytest ruff check .") == 0
        assert self._guard_rc("uv pip install pytest") == 0

    def test_the_over_read_is_confined_to_the_unconfident_walk(self):
        """Pins the residual the guard's docstring declares, and its bounds.

        Losing confidence means looking for a `pytest` token among the rest, so
        `echo pytest` behind an option NEITHER list can size is refused — an
        over-block `# full-suite-ok` clears. The two controls are the point: the
        same shape behind an option the guard's own list knows, and with no
        option at all, must still read `echo` as the command. If either flips,
        the unconfident scan has escaped the case it was written for.
        """
        assert self._guard_rc("uvx --allow-insecure-host h echo pytest") == 2
        assert self._guard_rc("uvx --directory /tmp echo pytest") == 0
        assert self._guard_rc("uvx echo pytest") == 0

    def test_an_option_unknown_to_both_lists_still_finds_the_run(self):
        assert self._guard_rc("uvx --allow-insecure-host h pytest") == 2
        assert self._guard_rc("uvx --allow-insecure-host h ruff check .") == 0
