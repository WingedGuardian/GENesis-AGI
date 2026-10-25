"""Registration gate's actual exec, descriptor and signal contracts."""

import fcntl
import os
import subprocess
import sys

import pytest

from genesis.hostmetrics import run


def _helper(command, *, report=None, env=None):
    reader, writer = os.pipe()
    incoming_env = os.environ if env is None else env
    try:
        child = subprocess.Popen(
            [sys.executable, '-I', '-S', '-c', run._SCOPE_START,
             str(writer), str(report) if report is not None else '-',
             '1' if 'LC_CTYPE' in incoming_env else '0', incoming_env.get('LC_CTYPE', ''),
             *command],
            pass_fds=(writer,) if report is None else (writer, report),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env,
        )
    finally:
        os.close(writer)
    try:
        proof = os.read(reader, 2)
        stdout, stderr = child.communicate(timeout=10)
        assert proof == b'1'
        assert os.read(reader, 1) == b''
        return child.returncode, stdout, stderr
    finally:
        os.close(reader)


@pytest.mark.parametrize('minimum', [3, 10, 30])
def test_report_fd_normalization_preserves_argv_stdout_and_status(minimum):
    reader, writer = os.pipe()
    high = fcntl.fcntl(writer, fcntl.F_DUPFD, minimum)
    os.close(writer)
    try:
        code, stdout, stderr = _helper(
            ['/bin/sh', '-c', run._REPORT_SH, 'genesis-job', '3',
             '/bin/sh', '-c', 'printf "%s|%s" "$1" "$2"; exit 7',
             'payload', 'space arg', 'literal;$()'], report=high,
        )
        assert code == 7
        assert stdout == b'space arg|literal;$()'
        assert stderr == b''
    finally:
        os.close(high)
    try:
        assert os.read(reader, 512).count(b'|') == 2
        assert os.read(reader, 1) == b''
    finally:
        os.close(reader)


def test_target_exec_receives_default_sigpipe():
    code, _, _ = _helper(['/bin/sh', '-c', 'kill -PIPE $$; exit 42'])
    assert code == -13


def test_no_site_hook_before_ack_and_payload_environment_preserved(tmp_path):
    marker = tmp_path / 'hook-ran'
    (tmp_path / 'sitecustomize.py').write_text(f'open({str(marker)!r}, "w").write("bad")')
    env = {**os.environ, 'PYTHONPATH': str(tmp_path), 'GENESIS_GATE_TEST': 'retained value'}
    code, stdout, _ = _helper(
        ['/bin/sh', '-c', 'printf "%s" "$GENESIS_GATE_TEST"'], env=env,
    )
    assert code == 0
    assert stdout == b'retained value'
    assert not marker.exists()


@pytest.mark.parametrize('ctype', [None, '', 'C', 'POSIX', 'C.UTF-8'])
def test_incoming_locale_environment_is_exec_transparent(ctype):
    env = {**os.environ, 'LANG': 'C', 'PYTHONCOERCECLOCALE': '0'}
    env.pop('LC_ALL', None)
    if ctype is None:
        env.pop('LC_CTYPE', None)
    else:
        env['LC_CTYPE'] = ctype
    command = ['/bin/sh', '-c', 'printf "%s|%s" "${LC_CTYPE+x}" "$LC_CTYPE"']
    direct = subprocess.run(command, env=env, capture_output=True, check=True)
    code, stdout, _ = _helper(command, env=env)
    assert code == 0
    assert stdout == direct.stdout
