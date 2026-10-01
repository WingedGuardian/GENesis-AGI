// Runs the dashboard's eval view-model functions against snapshots produced by
// the real eval_staleness() and prints what each would render.
//
// Input (stdin): JSON object mapping a case name to an eval_staleness snapshot.
// Output (stdout): JSON object mapping the same names to
//   {notice: <evalSnapshotNotice result>, profiles: [<evalProfileView result>]}.
//
// The assertions live in tests/test_observability/test_eval_staleness.py, which
// builds each snapshot from a real database, so no hand-written intermediate
// sits between the producer and this consumer.

const fs = require('fs');
const path = require('path');
const W = path.resolve(__dirname, '..', '..') + path.sep;

function extract(rel, startMark, endMark) {
  const src = fs.readFileSync(W + rel, 'utf8');
  const i = src.indexOf(startMark);
  if (i === -1) throw new Error('start marker not found in ' + rel + ': ' + startMark);
  const j = src.indexOf(endMark, i);
  if (j === -1) throw new Error('end marker not found in ' + rel + ': ' + endMark);
  return src.slice(i, j);
}

const body = extract('src/genesis/dashboard/templates/neural_monitor.html',
  'function evalSnapshotNotice(evalData) {', '\nasync function loadSurplusDetail');
const fns = new Function(body + '\nreturn {evalSnapshotNotice, evalProfileView};')();

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const out = {};
for (const [name, snap] of Object.entries(input)) {
  out[name] = {
    notice: fns.evalSnapshotNotice(snap),
    profiles: ((snap && snap.providers) || []).map(fns.evalProfileView),
  };
}
process.stdout.write(JSON.stringify(out));
