"""scripts/lib/timezone.sh: the installers' timezone read and clock set (#4879).

genesis_config_timezone reads the install's one timezone setting from genesis.yaml;
apply_system_timezone sets a clock and reports success only when the clock really
reads the zone afterwards. A stub `timedatectl` on PATH stands in for the real one,
so nothing here touches the machine's clock.
"""

import os
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
LIB = SCRIPTS / "lib" / "timezone.sh"


def _bash(script: str, *args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'. "{LIB}"\n{script}', "bash", *args],
        capture_output=True,
        text=True,
        env=env or dict(os.environ),
        timeout=60,
    )


@pytest.mark.parametrize(
    "content,expected",
    [
        (None, ""),  # no file
        ("github:\n  user: someone\n", ""),  # no key
        ('timezone: "UTC"\n', ""),  # the shipped placeholder
        ("timezone: Etc/UTC\n", ""),
        ('timezone: ""\n', ""),
        ('timezone: "Europe/Paris"\n', "Europe/Paris"),
        ("timezone: 'Asia/Tokyo'   # set from the dashboard\n", "Asia/Tokyo"),
        ("other:\n  timezone: Asia/Tokyo\n", ""),  # nested, not the top-level key
        ("# timezone: Asia/Tokyo\ntimezone: Pacific/Auckland\n", "Pacific/Auckland"),
        ("timezone: \"Europe/Paris\"\r\n", "Europe/Paris"),  # CRLF line ending
        ("timezone:Asia/Tokyo\n", "Asia/Tokyo"),  # no space after the colon
    ],
)
def test_genesis_config_timezone(tmp_path, content, expected):
    cfg = tmp_path / "genesis.yaml"
    if content is not None:
        cfg.write_text(content)
    out = _bash('genesis_config_timezone "$1"', str(cfg))
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == expected


def _stub_env(tmp_path: Path, *, set_ok: bool = True, reads_back: str | None = None) -> dict:
    """A stub timedatectl that records calls; `show` reads back the stored zone."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    state = tmp_path / "zone"
    calls = tmp_path / "calls"
    readback = f'echo "{reads_back}"' if reads_back is not None else f'cat "{state}"'
    (bindir / "timedatectl").write_text(
        "#!/bin/bash\n"
        f'echo "$*" >> "{calls}"\n'
        'if [ "$1" = set-timezone ]; then\n'
        + (f'  printf "%s" "$2" > "{state}"; exit 0\n' if set_ok else "  exit 1\n")
        + "fi\n"
        f'if [ "$1" = show ]; then {readback}; fi\n'
    )
    (bindir / "fakesudo").write_text(f'#!/bin/bash\necho "sudo $*" >> "{calls}"\nexec "$@"\n')
    for f in bindir.iterdir():
        f.chmod(0o755)
    return dict(os.environ, PATH=f"{bindir}:{os.environ['PATH']}")


def test_apply_reports_success_only_when_the_clock_reads_the_zone(tmp_path):
    env = _stub_env(tmp_path)
    out = _bash('apply_system_timezone "$1" Host fakesudo', "Europe/Paris", env=env)
    assert out.returncode == 0
    assert "+ Host timezone: Europe/Paris" in out.stdout
    assert "WARNING" not in out.stdout
    calls = (tmp_path / "calls").read_text()
    assert "sudo timedatectl set-timezone Europe/Paris" in calls  # the prefix was used


def test_apply_warns_when_the_set_fails(tmp_path):
    env = _stub_env(tmp_path, set_ok=False)
    out = _bash('apply_system_timezone "$1" Host fakesudo', "Europe/Paris", env=env)
    assert out.returncode == 1
    assert "+ Host timezone" not in out.stdout
    assert "WARNING: could not set the Host timezone to Europe/Paris" in out.stdout
    assert "fakesudo timedatectl set-timezone Europe/Paris" in out.stdout  # manual command


def test_apply_with_no_prefix_names_the_bare_command(tmp_path):
    env = _stub_env(tmp_path, set_ok=False)
    out = _bash('apply_system_timezone "$1" System', "Europe/Paris", env=env)
    assert out.returncode == 1
    assert "Set it manually: timedatectl set-timezone Europe/Paris" in out.stdout


def test_apply_is_safe_under_errexit_when_the_read_back_fails(tmp_path):
    # set succeeds, show fails: under set -euo pipefail with no caller guard the
    # function must still return 1 with a WARNING, not abort the script.
    env = _stub_env(tmp_path)
    (tmp_path / "bin" / "timedatectl").write_text(
        "#!/bin/bash\n[ \"$1\" = set-timezone ] && exit 0\nexit 3\n"
    )
    script = (
        "set -euo pipefail\n"
        'rc=0; apply_system_timezone "$1" System || rc=$?\n'
        'echo "rc=$rc"\n'
        "apply_system_timezone \"$1\" System\n"
        'echo "not reached"\n'
    )
    out = _bash(script, "Europe/Paris", env=env)
    assert "rc=1" in out.stdout
    assert "clock reads 'unknown' after setting Europe/Paris" in out.stdout
    assert "not reached" not in out.stdout  # an unguarded failure still stops set -e
    assert out.returncode == 1


def test_apply_warns_when_the_clock_does_not_read_back_the_zone(tmp_path):
    # The set command claims success but the clock still reads something else.
    env = _stub_env(tmp_path, reads_back="Etc/UTC")
    out = _bash('apply_system_timezone "$1" System', "Europe/Paris", env=env)
    assert out.returncode == 1
    assert "+ System timezone" not in out.stdout
    assert "WARNING: the System clock reads 'Etc/UTC' after setting Europe/Paris" in out.stdout


def test_installers_use_the_library():
    install = (SCRIPTS / "install.sh").read_text()
    # genesis.yaml is read before the deprecated secrets.env fallback.
    yaml_at = install.index('genesis_config_timezone "$HOME/.genesis/config/genesis.yaml"')
    secrets_at = install.index("^USER_TIMEZONE=")
    assert yaml_at < secrets_at
    assert 'apply_system_timezone "$GENESIS_TIMEZONE" "System" sudo' in install

    host = (SCRIPTS / "host-setup.sh").read_text()
    assert 'apply_system_timezone "$_final_tz" "Host" sudo' in host
    # The host leg no longer prints success unconditionally.
    assert 'echo "  + Timezone: $_final_tz (host)"' not in host
    assert 'sudo timedatectl set-timezone "$_final_tz" 2>/dev/null || true' not in host
