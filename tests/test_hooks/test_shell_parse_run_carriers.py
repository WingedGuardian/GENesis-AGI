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

    def test_uv_run_with_value_flags_before_command(self):
        # value-flags on `run` are consumed; the wrapped command is still found
        assert exes("uv run --python 3.12 pytest tests/") == ["pytest"]
        assert exes("uv run --with requests pytest tests/") == ["pytest"]

    def test_uv_run_double_dash_then_command(self):
        assert exes("uv run -- pytest tests/") == ["pytest"]

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

    def test_plain_forms_unchanged(self):
        assert self._guard_rc("pytest tests/") == 2
        assert self._guard_rc("pytest tests/test_x.py") == 0
