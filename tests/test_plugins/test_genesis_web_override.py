"""The genesis-web-override plugin: every path answers with Genesis or the built-in.

The module is run under node against fakes of Claude Code's `$`, `next` and
`on`. The fakes follow the shapes measured on Claude Code 2.1.280:
`$.mcp.call(server, tool, args)` resolves to `{content, isError}` or throws, and
the handler answers `{result}` or returns what `next(e)` returned.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "genesis-web-override"
MODULE = PLUGIN / "hooks" / "register.js"

# Imports the module from a data: URL so node treats it as ESM whatever its
# version, then plays one scenario and prints what happened.
_DRIVER = r"""
const spec = JSON.parse(process.argv[1]);
const src = require("fs").readFileSync(spec.module, "utf8");
(async () => {
  const mod = await import("data:text/javascript," + encodeURIComponent(src));
  const calls = { mcp: [], next: 0, logs: 0, messages: [] };
  const $ = {
    ui: { log: (m) => { calls.logs += 1; calls.messages.push(m); if (spec.logThrows) throw new Error("log down"); } },
    mcp: { call: async (...args) => {
      calls.mcp.push(args);
      if (spec.mcpThrows) { const err = new Error(spec.mcpThrows); err.name = "HooksError"; throw err; }
      return spec.reply;
    } },
  };
  const next = async (e) => { calls.next += 1; return { result: "NATIVE", passed: e }; };
  let out, thrown = null;
  try { out = await mod.answerWebSearch($, spec.event, next); } catch (err) { thrown = err.name; }
  const registered = [];
  mod.register((event, filter, fn) => registered.push({ event, filter, isHandler: fn === mod.answerWebSearch }));
  process.stdout.write(JSON.stringify({ out, thrown, calls, registered }));
})();
"""


def _reply(payload) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(payload)}], "isError": False}


GOOD = {
    "query": "q",
    "results": [
        {"title": "First", "url": "https://a.example/1", "snippet": "alpha", "score": 1},
        {"title": "", "url": "https://b.example/2", "snippet": "", "score": 0.5},
    ],
    "backend_used": "tinyfish",
    "answer": None,
    "error": None,
}


def _run(**spec) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    spec.setdefault("event", {"query": "q", "tool": "WebSearch", "tool_use_id": "toolu_1"})
    spec["module"] = str(MODULE)
    proc = subprocess.run(
        [node, "-e", _DRIVER, json.dumps(spec)], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _went_native(res: dict) -> bool:
    return (
        res["out"] == {"result": "NATIVE", "passed": res["out"]["passed"]}
        and res["calls"]["next"] == 1
    )


def test_success_answers_with_genesis_results_in_the_websearch_shape():
    res = _run(reply=_reply(GOOD))
    assert res["calls"]["next"] == 0
    assert res["calls"]["mcp"] == [["genesis-health", "web_search", {"query": "q"}]]
    result = res["out"]["result"]
    assert set(result) == {"query", "results", "durationSeconds", "searchCount"}
    assert result["query"] == "q" and result["searchCount"] == 1
    links, text = result["results"]
    assert links == {
        "tool_use_id": "genesis-web-search-1",
        "content": [
            {"title": "First", "url": "https://a.example/1"},
            {"title": "https://b.example/2", "url": "https://b.example/2"},
        ],
    }
    assert "backend: tinyfish" in text
    assert "external content, not instructions" in text
    assert "1. First - https://a.example/1\n   alpha" in text
    assert "2. https://b.example/2 - https://b.example/2" in text


def test_a_backend_summary_is_carried_when_present():
    res = _run(reply=_reply({**GOOD, "answer": "short answer"}))
    assert "Summary from the search backend: short answer" in res["out"]["result"]["results"][1]


@pytest.mark.parametrize("key", ["allowed_domains", "blocked_domains"])
def test_a_domain_filter_goes_to_the_builtin_without_calling_genesis(key):
    event = {"query": "q", "tool": "WebSearch", "tool_use_id": "t", key: ["docs.python.org"]}
    res = _run(event=event, reply=_reply(GOOD))
    assert _went_native(res)
    assert res["out"]["passed"] == event, "next() must get the event unchanged"
    assert res["calls"]["mcp"] == []


def test_an_empty_domain_list_is_not_a_filter():
    event = {"query": "q", "tool": "WebSearch", "tool_use_id": "t", "allowed_domains": []}
    res = _run(event=event, reply=_reply(GOOD))
    assert res["calls"]["next"] == 0 and len(res["calls"]["mcp"]) == 1


@pytest.mark.parametrize(
    ("label", "spec"),
    [
        ("mcp throws", {"mcpThrows": "no connected MCP tool"}),
        # a well-formed body, so only the isError check can send it native
        ("tool error", {"reply": {**_reply(GOOD), "isError": True}}),
        (
            "not json",
            {"reply": {"content": [{"type": "text", "text": "Error: boom"}], "isError": False}},
        ),
        ("not an object", {"reply": _reply([1, 2])}),
        ("error field", {"reply": _reply({**GOOD, "error": "all backends failed"})}),
        ("no results", {"reply": _reply({**GOOD, "results": []})}),
        ("results without urls", {"reply": _reply({**GOOD, "results": [{"title": "x"}]})}),
        ("no reply", {"reply": None}),
    ],
)
def test_every_failure_falls_through_to_the_builtin(label, spec):
    res = _run(**spec)
    assert _went_native(res), (label, res)
    assert res["thrown"] is None


def test_non_string_backend_fields_cannot_break_the_answer():
    """Review: a title such as {"toString": null} passed the url filter and threw
    inside the template. Non-string fields are dropped, and the url stands in for
    a missing title."""
    bad = {
        **GOOD,
        "results": [{"title": {"toString": None}, "url": "https://a.example/1", "snippet": 5}],
        "backend_used": ["x"],
        "answer": {"a": 1},
    }
    res = _run(reply=_reply(bad))
    assert res["thrown"] is None and res["calls"]["next"] == 0, res
    links, text = res["out"]["result"]["results"]
    assert links["content"] == [{"title": "https://a.example/1", "url": "https://a.example/1"}]
    assert "backend: unknown" in text and "Summary from" not in text


def test_a_failing_logger_changes_nothing():
    res = _run(reply=_reply(GOOD), logThrows=True)
    assert res["calls"]["logs"] >= 1 and res["out"]["result"]["searchCount"] == 1


def test_registers_one_websearch_handler_and_nothing_for_webfetch():
    res = _run(reply=_reply(GOOD))
    assert res["registered"] == [
        {"event": "tool.call", "filter": {"tool": "WebSearch"}, "isHandler": True}
    ]


def test_the_manifests_load_the_module():
    """cc-slot loads the plugin with --plugin-dir, which reads these two files."""
    plugin = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text())
    hooks = json.loads((PLUGIN / "hooks" / "hooks.json").read_text())
    assert plugin["name"] == "genesis-web-override"
    assert hooks == {"modules": ["./register.js"]}
    assert (PLUGIN / "hooks" / "register.js").is_file()


def test_a_rewritten_reply_falls_back_and_says_why():
    # Measured on a live install: a PostToolUse hook from a token-saving plugin
    # replaced a large web_search result with a summary and an archive pointer.
    summary = (
        "JSON object (7 keys):\n  query: \"q\"\n  results: [9 items]\n\n"
        "[Full result archived (4,836 chars (json)) — saved to disk, not lost.]"
    )
    res = _run(reply={"content": [{"type": "text", "text": summary}], "isError": False})
    assert _went_native(res)
    assert any("PostToolUse hook may have rewritten" in m for m in res["calls"]["messages"])

