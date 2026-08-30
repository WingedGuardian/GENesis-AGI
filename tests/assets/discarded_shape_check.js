// Executes all three discarded-queue normalisers against a table of payload
// shapes and checks each against an EXPECTED result, then checks that the three
// agree with each other.
//
// Both halves are necessary and neither is sufficient. Agreement alone passes
// when every implementation is equally wrong — replace all three with a stub
// returning zeros and a pure agreement check reports success. The expectations
// are the oracle; agreement is what catches one copy drifting from the others,
// which is how the two plain-JS monitor pages diverged from dashboard.js before.
//
// Run directly (`node tests/assets/discarded_shape_check.js`) or via
// tests/test_dashboard/test_webui_js_integrity.py.

const fs = require('fs');
const path = require('path');
// Repo root, derived from this file's location — never a hardcoded install
// path, so this runs from any clone, worktree, or CI checkout.
const W = path.resolve(__dirname, '..', '..') + path.sep;

function extract(rel, startMark, endMark) {
  const src = fs.readFileSync(W + rel, 'utf8');
  const i = src.indexOf(startMark);
  if (i === -1) throw new Error('start marker not found in ' + rel);
  const j = src.indexOf(endMark, i);
  if (j === -1) throw new Error('end marker not found in ' + rel);
  return src.slice(i, j);
}

const monitorSrc = extract('src/genesis/dashboard/templates/neural_monitor.html',
  'function normalizeDiscarded(queues) {', '\nfunction renderDiscardedItems');
const azSrc = extract('az_plugins/genesis/templates/neural_monitor.html',
  'function normalizeDiscarded(queues) {', '\nfunction renderDiscardedItems');

// dashboard.js declares it as an Alpine store getter over `this.health`.
const dashBody = extract('src/genesis/dashboard/webui/js/dashboard.js',
  '        get discarded() {', '        get discardedQueueGroups()')
  .replace('get discarded() {', 'function dashDiscarded() {')
  .replace(/\n        \},\s*$/, '\n}\n');

const impls = {
  monitor: new Function(monitorSrc + '\nreturn normalizeDiscarded;')(),
  az: new Function(azSrc + '\nreturn normalizeDiscarded;')(),
  dashboard: (function () {
    const f = new Function(dashBody + '\nreturn dashDiscarded;')();
    return (queues) => f.call({ health: { queues: queues } });
  })(),
};

const row = (n) => Array.from({ length: n }, (_, i) => ({ id: 'r' + i }));
const D = (o) => ({ discarded: o });

// [label, payload, expected {known, total, sampleLen, truncated}]
const CASES = [
  ['live: 148 deep, 20 sampled', D({ total: 148, sample: row(20), known: true, sample_truncated: true }),
    { known: true, total: 148, sampleLen: 20, truncated: true }],
  ['everything fits', D({ total: 3, sample: row(3), known: true, sample_truncated: false }),
    { known: true, total: 3, sampleLen: 3, truncated: false }],
  ['measured empty', D({ total: 0, sample: [], known: true, sample_truncated: false }),
    { known: true, total: 0, sampleLen: 0, truncated: false }],
  ['count known, sample failed', D({ total: 148, sample: [], known: true, sample_truncated: true }),
    { known: true, total: 148, sampleLen: 0, truncated: true }],
  ['count failed, few rows in hand', D({ total: 3, sample: row(3), known: false, sample_truncated: false }),
    { known: false, total: 3, sampleLen: 3, truncated: false }],
  // The case the producer used to get wrong: a full sample with no usable count.
  // total === sample.length, so comparing alone says "complete"; only the
  // producer's own flag knows the sample hit its cap.
  ['count failed, sample AT the cap', D({ total: 20, sample: row(20), known: false, sample_truncated: true }),
    { known: false, total: 20, sampleLen: 20, truncated: true }],
  ['backend flags truncation at equal counts', D({ total: 20, sample: row(20), known: true, sample_truncated: true }),
    { known: true, total: 20, sampleLen: 20, truncated: true }],
  // Defence in depth: a total below the rows shipped must not print below them.
  ['total lower than the sample', D({ total: 0, sample: row(3), known: true, sample_truncated: false }),
    { known: true, total: 3, sampleLen: 3, truncated: false }],
  ['object absent (older server)', {},
    { known: false, total: 0, sampleLen: 0, truncated: false }],
  ['whole queues section errored', { status: 'error', error: 'boom' },
    { known: false, total: 0, sampleLen: 0, truncated: false }],
  ['queues itself missing', undefined,
    { known: false, total: 0, sampleLen: 0, truncated: false }],
  ['total non-numeric', D({ total: 'n/a', sample: row(2), known: true }),
    { known: false, total: 2, sampleLen: 2, truncated: false }],
  ['total is an object', D({ total: {}, sample: row(2), known: true }),
    { known: false, total: 2, sampleLen: 2, truncated: false }],
  ['total null', D({ total: null, sample: row(2), known: true }),
    { known: false, total: 2, sampleLen: 2, truncated: false }],
  ['sample not an array', D({ total: 5, sample: 'nope', known: true }),
    { known: true, total: 5, sampleLen: 0, truncated: true }],
  ['known missing', D({ total: 5, sample: row(5) }),
    { known: false, total: 5, sampleLen: 5, truncated: false }],
  ['known truthy but not true', D({ total: 5, sample: row(5), known: 1 }),
    { known: false, total: 5, sampleLen: 5, truncated: false }],
];

const shape = (o) => (o && typeof o === 'object'
  ? { known: o.known, total: o.total, sampleLen: Array.isArray(o.sample) ? o.sample.length : null, truncated: o.truncated }
  : { THREW: String(o) });

let failures = 0;
for (const [label, payload, expected] of CASES) {
  const got = {};
  for (const [name, f] of Object.entries(impls)) {
    try { got[name] = shape(f(payload)); }
    catch (e) { got[name] = { THREW: e.message }; failures++; console.log(`THREW  ${label} [${name}]: ${e.message}`); }
  }
  const strs = Object.values(got).map((g) => JSON.stringify(g));
  const agree = strs.every((s) => s === strs[0]);
  const correct = JSON.stringify(got.dashboard) === JSON.stringify(expected);
  if (!agree) { failures++; console.log(`DISAGREE  ${label}\n    ${Object.entries(got).map(([k, v]) => k + '=' + JSON.stringify(v)).join('\n    ')}`); }
  if (!correct) { failures++; console.log(`WRONG     ${label}\n    expected ${JSON.stringify(expected)}\n    got      ${JSON.stringify(got.dashboard)}`); }
  if (agree && correct) console.log(`ok        ${label}`);
}

console.log(failures === 0
  ? `\nAll ${CASES.length} shapes: three implementations agree AND match expectations.`
  : `\n${failures} failure(s) across ${CASES.length} shapes.`);
process.exit(failures === 0 ? 0 : 1);
