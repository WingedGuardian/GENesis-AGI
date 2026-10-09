"""Run the real installer config phase against preserved scratch settings."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    "listener,backend,configured,port,expected,bind,nat,protocol",
    [
        ("tcp:127.0.0.1:5000", "tcp:127.0.0.1:5000", "", 5000, "127.0.0.1", "", "", ""),
        ("tcp:127.0.0.1:5000", "tcp:127.0.0.1:5000", "localhost", 5000, "localhost", "", "", ""),
        ("tcp:0.0.0.0:5000", "tcp:127.0.0.1:5000", "", 5000, "", "", "", ""),
        ("tcp:127.0.0.1:5000", "tcp:192.0.2.1:5000", "", 5000, "", "", "", ""),
        ("tcp:127.0.0.1:5000", "", "", 5000, "", "", "", ""),
        ("tcp:127.0.0.1:5000", "tcp:127.0.0.1:5000", "", 5555, "", "", "", ""),
        (
            "tcp:127.0.0.1:5000",
            "tcp:127.0.0.1:5000",
            "",
            5000,
            "127.0.0.1",
            "host",
            "false",
            "false",
        ),
        ("tcp:127.0.0.1:5000", "tcp:127.0.0.1:5000", "", 5000, "", "instance", "", ""),
        ("tcp:127.0.0.1:5000", "tcp:127.0.0.1:5000", "", 5000, "", "", "true", ""),
        ("tcp:127.0.0.1:5000", "tcp:127.0.0.1:5000", "", 5000, "", "", "", "true"),
        ("tcp:127.0.0.1:5000", "tcp:127.0.0.1:5000", "", 5000, "", "unreadable", "", ""),
    ],
)
def test_installer_aligns_unset_http_target_without_clobbering_config(
    tmp_path, listener, backend, configured, port, expected, bind, nat, protocol
):
    install = tmp_path / "guardian"
    (install / "config").mkdir(parents=True)
    (install / "src").symlink_to(_REPO / "src", target_is_directory=True)
    config = install / "config/guardian.yaml"
    config.write_text(
        f'# retain operator note\ncontainer_ip: "192.0.2.1"\nhealth_api_host: "{configured}"\nhealth_api_port: {port}\n'
    )
    script = (_REPO / "scripts/install_guardian.sh").read_text()
    target = script.split("# Auto-detect health API port", 1)[1].split(
        'VENV_DIR="$INSTALL_DIR/.venv"', 1
    )[0]
    target = "# Auto-detect health API port" + target
    phase = script.split("# ── Step 5:", 1)[1].split("# ── Step 6:", 1)[0]
    # Skip only the phase's descriptive heading, retaining its actual commands.
    phase = phase[phase.index('echo ""') :]
    body = (
        """set -euo pipefail
INSTALL_DIR=$1
VENV_DIR=$2
_listener=$3
_backend=$4
_bind=$5
_nat=$6
_protocol=$7
CONTAINER_NAME=fixture
incus() { case "${@: -1}" in listen) printf '%s\n' "$_listener" ;; connect) printf '%s\n' "$_backend" ;; bind) [ "$_bind" != unreadable ] || return 1; printf '%s\n' "$_bind" ;; nat) printf '%s\n' "$_nat" ;; proxy_protocol) printf '%s\n' "$_protocol" ;; *) return 1 ;; esac; }
"""
        + target
        + phase
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            body,
            "installer-config-test",
            str(install),
            str(Path(sys.executable).parent.parent),
            listener,
            backend,
            bind,
            nat,
            protocol,
        ],
        cwd=_REPO,
        env={"PATH": os.defpath, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    settings = yaml.safe_load(config.read_text())
    assert settings["health_api_host"] == expected
    assert settings["container_ip"] == "192.0.2.1"
    assert settings["health_api_port"] == port
    assert "# retain operator note" in config.read_text()


@pytest.mark.parametrize(
    "backend,expected",
    [
        ("tcp:127.0.0.1:5000", "already configured"),
        ("tcp:192.0.2.9:5000", "requires topology inspection"),
        ("", "requires topology inspection"),
    ],
)
def test_existing_host_proxy_checks_backend(backend, expected):
    script = (_REPO / "scripts/host-setup.sh").read_text()
    phase = script.split("# ── Dashboard port forwarding", 1)[1].split(
        "# ── Codebase visualization", 1
    )[0]
    phase = phase[phase.index("\n") :]
    body = (
        """set -euo pipefail
CONTAINER_NAME=fixture
_backend=$1
incus() { case "${@: -1}" in listen) echo tcp:127.0.0.1:5000 ;; connect) printf '%s\n' "$_backend" ;; bind|nat|proxy_protocol) printf '\n' ;; *) return 1 ;; esac; }
"""
        + phase
        + "\necho FOLLOWING_GUARDIAN_PHASE\n"
    )
    result = subprocess.run(
        ["bash", "-c", body, "proxy-test", backend], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == (0 if backend == "tcp:127.0.0.1:5000" else 1), result.stderr
    assert expected in result.stdout
    assert ("FOLLOWING_GUARDIAN_PHASE" in result.stdout) == (result.returncode == 0)
    if backend != "tcp:127.0.0.1:5000":
        assert "already configured" not in result.stdout


@pytest.mark.parametrize("layout", ["existing", "fresh", "partial", "same_directory"])
def test_code_copy_and_generation_preserve_operator_config(tmp_path, layout):
    source = tmp_path / "repo"
    (source / "src/genesis/guardian").mkdir(parents=True)
    (source / "src/genesis/__init__.py").touch()
    (source / "src/genesis/guardian/__init__.py").touch()
    for name in ("config.py", "dashboard_ingress.py"):
        shutil.copy2(_REPO / "src/genesis/guardian" / name, source / "src/genesis/guardian" / name)
    (source / "scripts").mkdir()
    (source / "scripts/shipped.sh").write_text("# shipped\n")
    (source / "config/nested").mkdir(parents=True)
    (source / "config/nested/default.yaml").write_text("setting: shipped\n")
    (source / "config/guardian.yaml").write_text("container_name: shipped-template\n")
    install = source if layout == "same_directory" else tmp_path / "installed"
    if layout in ("existing", "partial", "same_directory"):
        (install / "config").mkdir(parents=True, exist_ok=True)
        (install / "config/guardian.yaml").write_text(
            "# operator note\ncontainer_name: operator-container\n"
            'container_ip: "192.0.2.1"\nhealth_api_host: "localhost"\n'
        )
    if layout == "existing":
        (install / "src/genesis/guardian").mkdir(parents=True)
    script = (_REPO / "scripts/install_guardian.sh").read_text()
    copy_phase = script.split("# If already running from the install dir", 1)[1].split(
        "# ── Step 3:", 1
    )[0]
    # Restore the first comment line whose prefix was the extraction delimiter.
    copy_phase = "# If already running from the install dir" + copy_phase
    generation = script.split("# ── Step 5:", 1)[1].split("# ── Step 6:", 1)[0]
    generation = generation[generation.index('echo ""') :]
    body = (
        """set -euo pipefail
REPO_ROOT=$1
INSTALL_DIR=$2
PYTHON=$3
VENV_DIR=$4
CONTAINER_NAME=detected-container
CONTAINER_IP=192.0.2.2
HEALTH_HOST=127.0.0.1
HEALTH_PORT=5000
CC_ENABLED=false
CLAUDE_PATH=/fixture/claude
"""
        + copy_phase
        + generation
    )
    result = subprocess.run(
        [
            "bash",
            "-c",
            body,
            "installer-copy-test",
            str(source),
            str(install),
            sys.executable,
            str(Path(sys.executable).parent.parent),
        ],
        cwd=_REPO,
        env={"PATH": os.defpath, "HOME": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    content = (install / "config/guardian.yaml").read_text()
    settings = yaml.safe_load(content)
    if layout == "fresh":
        assert settings["container_name"] == "detected-container"
        assert settings["container_ip"] == "192.0.2.2"
        assert settings["health_api_host"] == "127.0.0.1"
    else:
        assert settings["container_name"] == "operator-container"
        assert settings["container_ip"] == "192.0.2.1"
        assert settings["health_api_host"] == "localhost"
        assert "# operator note" in content
    assert (install / "config/nested/default.yaml").read_text() == "setting: shipped\n"
    assert (install / "scripts/shipped.sh").read_text() == "# shipped\n"


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("existing", 0),
        ("fresh", 0),
        ("explicit_defaults", 0),
        ("add_failure", 1),
        ("readback_mismatch", 1),
        ("readback_failure", 1),
        ("reversed", 1),
        ("nat", 1),
        ("proxy_protocol", 1),
        ("mode_unreadable", 1),
    ],
)
def test_host_proxy_requires_readback_before_guardian(tmp_path, scenario, expected):
    script = (_REPO / "scripts/host-setup.sh").read_text()
    phase = script.split("# ── Dashboard port forwarding", 1)[1].split(
        "# ── Codebase visualization", 1
    )[0]
    phase = phase[phase.index("\n") :]
    calls = tmp_path / "calls"
    stub = r"""set -euo pipefail
CONTAINER_NAME=fixture
scenario=$1
calls=$2
added=0
incus() {
    if [ "$1" = list ]; then
        printf '%s\n' '[{"name":"fixture","devices":{},"expanded_devices":{"root":{"type":"disk"}}}]'
        return 0
    fi
    if [ "$1" = exec ]; then
        shift 3
        "$@"
        return $?
    fi
    echo "$3 ${@: -1}" >> "$calls"
    if [ "$3" = add ]; then
        [ "$scenario" != add_failure ] || { echo "fixture add failure" >&2; return 1; }
        added=1
        return 0
    fi
    [ "$3" = get ] || return 1
    case "$scenario" in fresh|add_failure|readback_mismatch|readback_failure) [ "$added" = 1 ] || return 1 ;; esac
    case "${@: -1}" in
        listen) echo tcp:127.0.0.1:5000 ;;
        connect)
            [ "$scenario" != readback_failure ] || { echo "fixture readback failure" >&2; return 1; }
            if [ "$scenario" = readback_mismatch ]; then echo tcp:192.0.2.9:5000; else echo tcp:127.0.0.1:5000; fi ;;
        bind)
            [ "$scenario" != mode_unreadable ] || { echo "fixture mode failure" >&2; return 1; }
            case "$scenario" in reversed) echo instance ;; explicit_defaults) echo host ;; *) echo ;; esac ;;
        nat) case "$scenario" in nat) echo true ;; explicit_defaults) echo false ;; *) echo ;; esac ;;
        proxy_protocol) case "$scenario" in proxy_protocol) echo true ;; explicit_defaults) echo false ;; *) echo ;; esac ;;
        *) return 1 ;;
    esac
}
"""
    result = subprocess.run(
        [
            "bash",
            "-c",
            stub + phase + "\necho FOLLOWING_GUARDIAN_PHASE\n",
            "proxy-test",
            scenario,
            str(calls),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == expected, result.stderr
    assert ("FOLLOWING_GUARDIAN_PHASE" in result.stdout) == (expected == 0)
    trace = calls.read_text()
    assert "set " not in trace and "remove " not in trace
    if scenario in {
        "existing",
        "explicit_defaults",
        "reversed",
        "nat",
        "proxy_protocol",
        "mode_unreadable",
    }:
        assert "add " not in trace
    if scenario in {"add_failure", "readback_failure", "mode_unreadable"}:
        assert "fixture" in result.stderr


@pytest.mark.parametrize("optional", [False, True])
def test_generated_network_identity_describes_loopback_access(optional):
    library = _REPO / "scripts/lib/claude_md_blocks.sh"
    result = subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; build_network_identity_block "$2" "$3" "$4" "$5" "$6"',
            "identity-test",
            str(library),
            "192.0.2.3",
            "2001:db8::3" if optional else "",
            "192.0.2.4",
            "2001:db8::4" if optional else "",
            "192.0.2.5" if optional else "",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert "**Container IP**: 192.0.2.3" in result.stdout
    assert "**Host VM IP**: 192.0.2.4" in result.stdout
    assert ("**Tailscale**" in result.stdout) == optional
    assert ("2001:db8::3" in result.stdout) == optional
    assert ("2001:db8::4" in result.stdout) == optional
    dashboard = next(line for line in result.stdout.splitlines() if "**Dashboard**" in line)
    assert "http://127.0.0.1:5000" in dashboard and "SSH tunnel" in dashboard
    assert "authenticated, ACL-restricted HTTPS" in dashboard
    assert "192.0.2.4:5000" not in dashboard


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("absent", 0),
        ("inherited", 1),
        ("local_unreadable", 1),
        ("missing_map", 1),
        ("null_map", 1),
        ("missing_instance", 1),
        ("duplicate_instance", 1),
        ("malformed", 1),
        ("list_failed", 1),
        ("exec_failed", 1),
    ],
)
def test_host_setup_proves_absence_before_any_add(tmp_path, scenario, expected):
    import json

    row = {"name": "fixture", "devices": {}, "expanded_devices": {"root": {"type": "disk"}}}
    if scenario == "inherited":
        row["expanded_devices"]["dashboard-proxy"] = {"type": "proxy"}
    elif scenario == "local_unreadable":
        row["devices"]["dashboard-proxy"] = {"type": "proxy"}
    elif scenario == "missing_map":
        del row["expanded_devices"]
    elif scenario == "null_map":
        row["devices"] = None
    metadata = (
        []
        if scenario == "missing_instance"
        else [row, row]
        if scenario == "duplicate_instance"
        else [row]
    )
    listing = tmp_path / "metadata.json"
    listing.write_text("{" if scenario == "malformed" else json.dumps(metadata))
    calls = tmp_path / "calls"
    script = (_REPO / "scripts/host-setup.sh").read_text()
    phase = script.split("# ── Dashboard port forwarding", 1)[1].split(
        "# ── Codebase visualization", 1
    )[0]
    phase = phase[phase.index("\n") :]
    stub = r"""set -euo pipefail
CONTAINER_NAME=fixture
scenario=$1
listing=$2
calls=$3
added=0
incus() {
    if [ "$1" = list ]; then
        cat "$listing"
        [ "$scenario" != list_failed ] || return 1
        return 0
    fi
    if [ "$1" = exec ]; then
        [ "$scenario" != exec_failed ] || return 1
        shift 3
        "$@"
        return $?
    fi
    echo "$3 ${@: -1}" >> "$calls"
    if [ "$3" = add ]; then added=1; return 0; fi
    [ "$3" = get ] && [ "$added" = 1 ] || return 1
    case "${@: -1}" in listen|connect) echo tcp:127.0.0.1:5000 ;; bind|nat|proxy_protocol) echo ;; *) return 1 ;; esac
}
"""
    result = subprocess.run(
        [
            "bash",
            "-c",
            stub + phase + "\necho FOLLOWING_GUARDIAN_PHASE\n",
            "absence-test",
            scenario,
            str(listing),
            str(calls),
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == expected, result.stderr
    assert ("FOLLOWING_GUARDIAN_PHASE" in result.stdout) == (expected == 0)
    trace = calls.read_text()
    assert ("add " in trace) == (expected == 0)
    assert "set " not in trace and "remove " not in trace
