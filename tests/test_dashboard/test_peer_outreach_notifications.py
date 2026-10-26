"""Exercise the actual outreach fetch method with mixed notification history."""

import json
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("mode", ["mixed", "all_cli", "empty"])
def test_generic_outreach_excludes_cli_notifications_and_peer_consent(mode):
    source = Path(__file__).resolve().parents[2] / "src/genesis/dashboard/webui/js/dashboard.js"
    script = r"""
const fs = require('fs');
const vm = require('vm');
const assert = require('assert/strict');
const [sourcePath, mode] = process.argv.slice(1);
const source = fs.readFileSync(sourcePath, 'utf8');
const start = source.indexOf('async fetchOutreachMessages()');
const end = source.indexOf('// Find a pending approval', start);
assert(start >= 0 && end > start);
const ordinary = [
  {id:'blocker', category:'blocker', signal_type:'ordinary'},
  {id:'approval', category:'approval', signal_type:'ordinary'},
  {id:'alert', category:'alert'}, {id:'digest', category:'digest'},
];
const cli = [
  {id:'peer-notification', category:'approval', signal_type:'cli_approval'},
  {id:'ordinary-cli', category:'approval', signal_type:'cli_approval'},
];
const rows = mode === 'mixed' ? [cli[0], ...ordinary, cli[1]] : mode === 'all_cli' ? cli : [];
const approvals = [{id:'ordinary', action_type:'ordinary'}, {id:'peer', action_type:'peer_operation'}];
const calls = [];
const methods = vm.runInNewContext('({' + source.slice(start, end) + '})', {
  fetchApi: async path => {
    calls.push(path);
    return {ok:true, json:async () => path.includes('outreach/messages') ? rows : approvals};
  },
});
const states = [];
const view = {
  outreachModal:{},
  startModalFetch:name => states.push('start:' + name),
  finishModalFetch:name => states.push('finish:' + name),
  failModalFetch:() => assert.fail('unexpected fetch failure'),
};
(async () => {
  await methods.fetchOutreachMessages.call(view);
  assert.deepEqual(Array.from(view.outreachModal.messages), mode === 'mixed' ? ordinary : []);
  assert.deepEqual(Array.from(view.outreachModal.pendingApprovals), [approvals[0]]);
  assert.deepEqual(states, ['start:outreachModal', 'finish:outreachModal']);
  assert.equal(calls.length, 2);
  console.log(JSON.stringify({mode, status:'PASS'}));
})().catch(() => { console.error('outreach notification control failed'); process.exit(1); });
"""
    result = subprocess.run(
        ["node", "-e", script, str(source), mode], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"mode": mode, "status": "PASS"}
