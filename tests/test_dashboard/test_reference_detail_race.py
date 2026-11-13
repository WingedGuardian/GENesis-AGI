"""Run the real reference detail and reveal methods against deferred fetches.

A detail or reveal response that arrives after the view closed, or after
another reference was opened, must be discarded.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

_SOURCE = Path(__file__).parents[2] / "src/genesis/dashboard/webui/js/dashboard.js"
_METHODS = {
    "viewReferenceDetail": "async viewReferenceDetail(id) {",
    "revealReference": "async revealReference(id) {",
    "hideReference": "hideReference(id) {",
    "closeReferenceDetail": "closeReferenceDetail() {",
    "deleteReference": "async deleteReference(id) {",
}

_HARNESS = r"""
const AsyncFunction = Object.getPrototypeOf(async function(){}).constructor;
const pending = {};
globalThis.fetchApi = (url) => new Promise(resolve => {
  (pending[url] = pending[url] || []).push(resolve);
});
const store = {
  referenceDetail: null, revealedValues: {}, revealedDescriptions: {},
  revealTokens: {}, revealErrors: {}, _revealSeq: 0, _detailSeq: 0, _detailId: null,
  referenceSearchResults: [], showConfirm: async () => true,
  fetchReferenceList: async () => {}, fetchReferenceStats: async () => {},
};
for (const [name, [isAsync, args, body]] of Object.entries(METHODS)) {
  store[name] = (isAsync ? new AsyncFunction(...args, body) : new Function(...args, body)).bind(store);
}
const detailOk = id => ({ok: true, status: 200, json: async () => ({reference: {id}})});
const resolve = (url, resp) => pending[url].shift()(resp);
const detail = id => `/api/genesis/references/${id}`;
const tick = () => new Promise(r => setTimeout(r, 0));
const out = {};
(async () => {
  // Open A then B; B answers first, A last: the view shows B.
  let a = store.viewReferenceDetail("A"); let b = store.viewReferenceDetail("B");
  resolve(detail("B"), detailOk("B")); await b;
  resolve(detail("A"), detailOk("A")); await a;
  out.outOfOrder = store.referenceDetail && store.referenceDetail.id;
  // Close before the response arrives: the view stays closed.
  store.closeReferenceDetail();
  let c = store.viewReferenceDetail("C"); store.closeReferenceDetail();
  resolve(detail("C"), detailOk("C")); await c;
  out.closedStaysClosed = store.referenceDetail;
  // A failed load leaves no requested id behind.
  let d = store.viewReferenceDetail("D");
  resolve(detail("D"), {ok: false, status: 503, json: async () => ({})}); await d;
  out.failedLoadId = store._detailId;
  // A reveal pending at close is discarded.
  let e = store.viewReferenceDetail("E"); resolve(detail("E"), detailOk("E")); await e;
  let r = store.revealReference("E"); await tick(); store.closeReferenceDetail();
  resolve(detail("E") + "/reveal", {ok: true, status: 200, json: async () => ({value: "v", description: "d"})});
  await r;
  out.revealAfterClose = [store.revealedValues.E ?? null, store.revealedDescriptions.E ?? null];
  // Delete F, close it and open G while the DELETE is pending; the DELETE
  // finishing first must not drop G's detail response.
  let f = store.viewReferenceDetail("F"); resolve(detail("F"), detailOk("F")); await f;
  let del = store.deleteReference("F"); await tick();
  store.closeReferenceDetail();
  let g = store.viewReferenceDetail("G");
  resolve(detail("F"), {ok: true, status: 200, json: async () => ({})}); await del;
  resolve(detail("G"), detailOk("G")); await g;
  out.openDuringDelete = store.referenceDetail && store.referenceDetail.id;
  // A DELETE finishing after another reference already loaded leaves it open.
  let h = store.viewReferenceDetail("H"); resolve(detail("H"), detailOk("H")); await h;
  let del2 = store.deleteReference("H"); await tick();
  store.closeReferenceDetail();
  let k = store.viewReferenceDetail("K"); resolve(detail("K"), detailOk("K")); await k;
  resolve(detail("H"), {ok: true, status: 200, json: async () => ({})}); await del2;
  out.loadedDuringDelete = store.referenceDetail && store.referenceDetail.id;
  // Deleting the open reference still closes it.
  let m = store.viewReferenceDetail("M"); resolve(detail("M"), detailOk("M")); await m;
  let del3 = store.deleteReference("M"); await tick();
  resolve(detail("M"), {ok: true, status: 200, json: async () => ({})}); await del3;
  out.deletedOpenCloses = store.referenceDetail;
  console.log(JSON.stringify(out));
})().catch(err => { console.error(err); process.exitCode = 1; });
"""


def _methods() -> dict:
    source = _SOURCE.read_text()
    found = {}
    for name, signature in _METHODS.items():
        start = source.index(signature) + len(signature)
        body = source[start : source.index("\n        },", start)]
        args = signature[signature.index("(") + 1 : signature.index(")")]
        found[name] = [signature.startswith("async"), [a for a in [args] if a], body]
    return found


def test_late_detail_and_reveal_responses_are_discarded():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not installed")
    script = f"const METHODS = {json.dumps(_methods())};\n" + _HARNESS
    ran = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert ran.returncode == 0, ran.stderr
    result = json.loads(ran.stdout)
    assert result["outOfOrder"] == "B"
    assert result["closedStaysClosed"] is None
    assert result["failedLoadId"] is None
    assert result["revealAfterClose"] == [None, None]
    assert result["openDuringDelete"] == "G"
    assert result["loadedDuringDelete"] == "K"
    assert result["deletedOpenCloses"] is None
