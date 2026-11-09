"""The scanner process receives the native environment preserved by the chain."""
import json

import pytest

from tests.test_hooks.test_native_push_privacy import native_world as native_world


@pytest.mark.parametrize('selected_runtime', [False, True])
@pytest.mark.parametrize('safe_path', [False, True])
def test_scanner_receives_initial_native_environment(native_world, selected_runtime, safe_path):
    repo, env, git, commit, invoke = native_world
    if not selected_runtime:
        (repo / '.venv').unlink()
    for name in ('LC_CTYPE', 'LC_ALL', 'PYTHONCOERCECLOCALE', 'PYTHONSAFEPATH'):
        env.pop(name, None)
    env['LANG'] = 'C'
    env['R8_SYNTHETIC_SENTINEL'] = 'owned=value'
    if safe_path:
        env['PYTHONSAFEPATH'] = '1'
    scanner = repo / 'scripts/hooks/push_privacy_scan.py'
    # This owned consumer proves the forwarding boundary only. The other
    # native tests exercise the actual scanner; no synthetic verdict is used
    # as evidence of privacy enforcement.
    scanner.write_text('import json, sys\nfrom pathlib import Path\n'
                       'env = dict(item.split(b"=", 1) for item in Path("/proc/self/environ").read_bytes().split(b"\\0") if item)\n'
                       'keys = (b"LC_CTYPE", b"PYTHONSAFEPATH", b"R8_SYNTHETIC_SENTINEL")\n'
                       'Path("scanner-environment.json").write_text(json.dumps({key.decode(): env.get(key, b"ABSENT").decode() for key in keys}))\n'
                       'assert sys.argv[3] == "--"\n'
                       'assert sys.stdin.buffer.read().endswith(b"\\n")\n')
    result = invoke(commit('owned safe message'))
    assert result.returncode == 0, result.stderr
    assert json.loads((repo / 'scanner-environment.json').read_text()) == {
        'LC_CTYPE': 'ABSENT', 'PYTHONSAFEPATH': '1' if safe_path else 'ABSENT',
        'R8_SYNTHETIC_SENTINEL': 'owned=value',
    }
