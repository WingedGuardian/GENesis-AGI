"""Exercise the actual dashboard action consumer with operation responses."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    "response,expects_error",
    [
        ({"http_ok": True, "body": {"success": False, "error": "registration pending"}}, True),
        ({"http_ok": True, "body": {"success": True}}, False),
        ({"http_ok": False, "body": {"error": "not available"}}, True),
        (None, True),
        ({"http_ok": True, "body": {}}, True),
    ],
)
def test_dashboard_action_reads_operation_result(response, expects_error):
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    source = (Path(__file__).parents[2] / "src/genesis/dashboard/webui/js/dashboard.js").read_text()
    signature = "async userJobAction(jobId, action) {"
    start = source.index(signature) + len(signature)
    body = source[start:source.index("\n        },", start)]
    script = (
        "const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;\n"
        f"const action = new AsyncFunction('jobId','action','fetchApi','alert',{json.dumps(body)});\n"
        f"const response = {json.dumps(response)};\n"
        "const alerts=[]; let refreshes=0;\n"
        "const self={fetchSchedulerData:async()=>{refreshes++;}};\n"
        "const fetchApi=async()=>response && ({ok:response.http_ok,json:async()=>response.body});\n"
        "action.call(self,'synthetic-id','resume',fetchApi,msg=>alerts.push(msg))"
        ".then(()=>console.log(JSON.stringify({alerts,refreshes})))"
        ".catch(e=>{console.error(e);process.exitCode=1;});\n"
    )
    ran = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert ran.returncode == 0, ran.stderr
    result = json.loads(ran.stdout)
    assert bool(result["alerts"]) == expects_error
    assert result["refreshes"] == 1
