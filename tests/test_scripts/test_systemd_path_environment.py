"""Every rendered Environment= line that carries an install path stays ONE assignment.

systemd splits an unquoted Environment= value on whitespace, so
``Environment=PATH=/a b:/c`` sets ``PATH=/a`` and drops ``b:/c`` as an invalid
assignment. Unit templates interpolate install-derived directories (the venv,
$HOME, the clone, the Claude Code bin dir) into Environment= lines, and any of
them can contain a space or a character systemd's quoting treats specially. On
one WSL install the CC bin dir was a Windows npm dir,
``/mnt/c/Users/<First Last>/AppData/Roaming/npm``: genesis-server ran with PATH
cut at the space, missing /usr/bin, so pgrep, gh and systemctl were "not found".

genesis-code-intel.service.template already used the safe shape: the whole
assignment in double quotes, with the ``__*_EXEC__`` tokens, which the renderers
escape for systemd (``\\`` and ``"`` backslashed, ``%`` doubled). This pins that
shape for every install-derived Environment= line, through both real renderers.
(Exec*= argv lines are a different class and are not covered here.)
"""

from __future__ import annotations

import re
import shlex
import shutil
from pathlib import Path

import pytest

from tests.test_scripts.test_codebase_managed_templates import render as render

_ROOT = Path(__file__).resolve().parents[2]
_TEMPLATE_DIR = _ROOT / "scripts" / "systemd"
_INSTALL_TOKEN = re.compile(r"__(?:HOME|VENV|REPO|CC_BIN)(?:_DIR|_EXEC)?__")
_PLAIN_TOKEN = re.compile(r"__(?:HOME|VENV|REPO_DIR|CC_BIN_DIR)__")


def _install_env_lines(text: str) -> list[str]:
    # Environment= only: EnvironmentFile= takes a path, not assignments, and its
    # quoting belongs with the Exec*= path class (#3226).
    return [line for line in text.splitlines()
            if line.startswith("Environment=") and _INSTALL_TOKEN.search(line)]


def _templates() -> list[str]:
    return [t.name for t in sorted(_TEMPLATE_DIR.glob("*.service.template"))
            if _install_env_lines(t.read_text(encoding="utf-8"))]


_TEMPLATES = _templates()


def test_the_scan_finds_the_templates_that_carry_install_paths():
    # Guard the guard: an empty or shrunken list would make the tests below pass
    # vacuously. These carry install-derived Environment= lines today.
    for name in ("genesis-server.service.template", "genesis-code-intel.service.template",
                 "genesis-inbox-sync.service.template"):
        assert name in _TEMPLATES, name


@pytest.mark.parametrize("template_name", _TEMPLATES)
def test_install_paths_in_environment_lines_are_quoted_and_escaped(template_name):
    for line in _install_env_lines((_TEMPLATE_DIR / template_name).read_text(encoding="utf-8")):
        assert line.startswith('Environment="') and line.endswith('"'), (
            f"{template_name}: unquoted install path splits at a space: {line}")
        assert not _PLAIN_TOKEN.search(line), (
            f"{template_name}: use the __*_EXEC__ token inside quotes "
            f"(the plain one is not escaped for systemd): {line}")


@pytest.mark.parametrize("template_name", _TEMPLATES)
@pytest.mark.parametrize("renderer", ["install.sh", "bootstrap.sh"])
def test_rendered_environment_survives_spaces_and_metacharacters(render, tmp_path, renderer,
                                                                 template_name):
    units, run = render
    shutil.copy2(_TEMPLATE_DIR / template_name, units.parent / "templates")
    home = tmp_path / 'home $%λ" \\ '
    cc_bin = tmp_path / "Users/First Last/AppData/Roaming/npm"
    result = run(renderer, home, cc_bin_dir=str(cc_bin))
    assert result.returncode == 0, result.stdout + result.stderr
    # The same venv each renderer's own test derives (test_code_intel_service_template).
    venv = home / ".venv" if renderer == "bootstrap.sh" else tmp_path / 'venv=$%&|λ" '

    template_lines = _install_env_lines((_TEMPLATE_DIR / template_name).read_text(encoding="utf-8"))
    rendered = [line for line in (units / template_name.removesuffix(".template"))
                .read_text(encoding="utf-8").splitlines() if line.startswith("Environment=")]
    keys = {line.split("=", 2)[1].lstrip('"') for line in template_lines}
    checked = 0
    for line in rendered:
        raw = line.removeprefix("Environment=")
        # systemd's Environment= tokenizer splits on whitespace and honours double
        # quotes with backslash escapes, as shlex does for this alphabet. More than
        # one token means systemd would cut the value at the first space.
        assignments = shlex.split(raw)
        key, _, value = assignments[0].replace("%%", "%").partition("=")
        if key not in keys:
            continue
        checked += 1
        assert len(assignments) == 1, (
            f"{template_name}: {key} renders as {len(assignments)} assignments under "
            f"{renderer}: {raw!r}")
        assert str(home) in value or str(venv) in value or str(cc_bin) in value, (key, value)
        if key == "PATH":
            entries = value.split(":")
            assert entries[0] == f"{venv}/bin"
            assert entries[-3:] == ["/usr/local/bin", "/usr/bin", "/bin"]
            template_line = next(t for t in template_lines if "PATH=" in t)
            if "__CC_BIN" in template_line:
                assert str(cc_bin) in entries
            if "__HOME" in template_line:
                assert f"{home}/.npm-global/bin" in entries
    assert checked == len(template_lines), (checked, template_lines)
