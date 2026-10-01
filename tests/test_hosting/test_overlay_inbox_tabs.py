"""genesis-overlay.js inbox modal: status tabs must query the server by status.

The modal used to fetch the newest 50 rows once and filter them client-side, so
any status whose rows were older than 50 newer rows of OTHER statuses showed an
empty tab. The behavioural test runs the real functions (extracted from the
shipped file) in node against a minimal fake DOM and a fake fetch whose newest
window holds no failed rows at all.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

OVERLAY_JS = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "genesis"
    / "hosting"
    / "agent_zero"
    / "static"
    / "genesis-overlay.js"
)


def _extract(src: str, header: str) -> str:
    """Return the top-level declaration starting at ``header`` (brace-matched)."""
    start = src.index(header)
    if header.startswith("const "):
        return src[start : src.index(";\n", start) + 1]
    depth = 0
    i = src.index("{", start)
    while True:
        ch = src[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
        i += 1


_HARNESS = r"""
class Node {
  constructor(tag) { this.tag = tag; this.children = []; this.className = "";
    this.textContent = ""; this.dataset = {}; this.id = ""; this.listeners = {};
    const self = this;
    this.classList = { add(c) { self.className += " " + c; },
                       remove(c) { self.className = self.className.replace(c, ""); } }; }
  appendChild(c) { this.children.push(c); return c; }
  replaceChildren(...cs) { this.children = cs; }
  addEventListener(t, fn) { this.listeners[t] = fn; }
  remove() {}
  allText() { return [this.textContent, ...this.children.map((c) => c.allText())].join(" "); }
  find(pred) { if (pred(this)) return this;
    for (const c of this.children) { const f = c.find(pred); if (f) return f; } return null; }
}
const body = new Node("body");
globalThis.document = { createElement: (t) => new Node(t), getElementById: () => null, body };
globalThis.requestAnimationFrame = () => {};
globalThis.setTimeout = () => {};
const requested = [];
const newest = Array.from({ length: 50 }, (_, i) => (
  { status: "superseded", created_at: "t" + i, file_path: "/inbox/new" + i + ".md" }));
globalThis.fetch = async (url) => {
  requested.push(url);
  let rows = newest;
  if (url.includes("status=failed")) rows = [{ status: "failed", created_at: "t-old", file_path: "/inbox/OLDFAIL.md" }];
  else if (url.includes("status=")) rows = [];
  return { ok: true, json: async () => rows };
};
globalThis.console.warn = () => {};
__FUNCS__
(async () => {
  await openGenesisInboxModal();
  const failedTab = body.find((n) => n.tag === "button" && n.dataset.filter === "failed");
  await failedTab.listeners.click();
  await new Promise((r) => setImmediate(r));
  const text = body.allText();
  process.stdout.write(JSON.stringify({ requested, hasOldFail: text.includes("OLDFAIL.md") }));
})().catch((e) => { console.error(e); process.exit(1); });
"""


def test_inbox_url_builder_and_status_tab_query_server():
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    src = OVERLAY_JS.read_text()
    funcs = "\n".join(
        _extract(src, h)
        for h in (
            "async function fetchJson(",
            "function el(",
            "const INBOX_FILTERS",
            "function inboxUrlForFilter(",
            "async function openGenesisInboxModal(",
            "function closeGenesisModal(",
        )
    )
    script = _HARNESS.replace("__FUNCS__", funcs)
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[:1500]
    out = json.loads(r.stdout)
    assert out["requested"][0] == "/api/genesis/ui/inbox?limit=50"
    assert "/api/genesis/ui/inbox?limit=50&status=failed" in out["requested"]
    # The failed row is OLDER than the 50-row mixed window, yet the tab shows it.
    assert out["hasOldFail"], out


def test_every_status_tab_is_a_real_inbox_status():
    """The tab list must name only statuses the inbox_items CHECK allows."""
    src = OVERLAY_JS.read_text()
    tabs = json.loads(re.search(r"const INBOX_FILTERS = (\[[^\]]*\]);", src).group(1))
    tables = (
        Path(__file__).resolve().parents[2] / "src" / "genesis" / "db" / "schema" / "_tables.py"
    ).read_text()
    block = tables[tables.index('"inbox_items": """') :]
    check = re.search(r"status IN \(([^)]*)\)", block).group(1)
    allowed = set(re.findall(r"'([a-z_]+)'", check))
    assert tabs[0] == "all"
    assert set(tabs[1:]) <= allowed, (tabs, allowed)
