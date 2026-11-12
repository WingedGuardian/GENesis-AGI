"""genesis-secret-redact's mod (hooks/register.js), run under node against a fake of Claude Code's engine.

The fake `$` reads and writes real scratch files and runs the real loader (bin/known_values.py) with
SECRETS_PATH pointed at a scratch secrets file, so what is exercised is the mod and the loader together. The
handler shapes follow what was measured on Claude Code 2.1.299: a handler answers `{result}` /
`{result, context}` / `{deny}` or returns what `next` returned. Canary values only.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

PLUGIN = Path(__file__).resolve().parents[2] / "plugins" / "genesis-secret-redact"
KEY = "ck_" + "a1b2c3d4e5f60718293a4b5c6d7e8f90"
KEY2 = "ck_" + "0f9e8d7c6b5a41322314a5b6c7d8e9f0"
P = "[secrets.env:CANARY_API_KEY]"

# Imports the module from a data: URL (so node treats it as ESM whatever its version), wires the fakes and runs
# one scenario: `script` is the body of an async function that gets the helpers as `t`.
_DRIVER = r"""
const spec = JSON.parse(process.argv[1]);
const fs = require("fs");
const { execFile } = require("child_process");
(async () => {
  const mod = await import("data:text/javascript," + encodeURIComponent(fs.readFileSync(spec.module, "utf8")));
  const logs = [];
  let loaderRuns = 0, failLoads = 0, failResolves = 0, truncate = 0, now = Date.now(), tick = 0;
  Date.now = () => now;
  const $ = {
    plugin: { root: spec.plugin },
    ui: { log: m => logs.push(m) },
    fs: {
      stat: async p => { const st = fs.statSync(p); return { kind: st.isFile() ? "file" : "dir", mtimeMs: st.mtimeMs, size: st.size }; },
      read: async p => fs.readFileSync(p, "utf8"),
      write: async (p, text) => fs.writeFileSync(p, text),
    },
    process: { run: argv => new Promise(resolve => {
      if (argv[0] === "realpath") {
        if (failResolves > 0) { failResolves -= 1; return resolve({ exitCode: 1, stdout: "", stderr: "" }); }
      } else {
        loaderRuns += 1;
        if (failLoads > 0) { failLoads -= 1; return resolve({ exitCode: 1, stdout: "", stderr: "" }); }
      }
      // HOME is the scratch directory, so `~/secrets.env` names the scratch secrets file.
      execFile(argv[0], argv.slice(1), { env: { ...process.env, SECRETS_PATH: spec.secrets, HOME: spec.dir }, encoding: "utf8" },
        (err, stdout, stderr) => {
          const cut = argv[0] !== "realpath" && truncate > 0 && (truncate -= 1, true);
          resolve({ exitCode: err ? (err.code ?? 1) : 0, stdout, stderr, isStdoutTruncated: cut });
        });
    }) },
  };
  const handlers = {}, catchers = {};
  mod.register((name, fn) => { handlers[name] = fn; return { catch: c => { catchers[name] = c; } }; });
  const t = {
    v: spec.vars, secrets: spec.secrets, dir: spec.dir, logs, catchers, $,
    emit: (name, e, next = async x => x) => handlers[name]($, e, next),
    // Each write gets a later mtime, so the mod's stamp differs whatever the clock's resolution.
    write: text => { fs.writeFileSync(spec.secrets, text); tick += 1; const d = new Date(now + tick * 5000); fs.utimesSync(spec.secrets, d, d); },
    read: p => fs.readFileSync(p ?? spec.secrets, "utf8"),
    put: (p, text) => { fs.mkdirSync(require("path").dirname(p), { recursive: true }); fs.writeFileSync(p, text); },
    advance: ms => { now += ms; },
    failNext: n => { failLoads = n; },
    failResolve: n => { failResolves = n; },
    truncateNext: n => { truncate = n; },
    link: (target, p) => { fs.mkdirSync(require("path").dirname(p), { recursive: true }); fs.symlinkSync(target, p); },
    runs: () => loaderRuns,
  };
  const out = await new Function("t", `return (async () => { ${spec.script} })()`)(t);
  process.stdout.write(JSON.stringify({ out, logs }));
})().catch(err => { console.error(err); process.exit(1); });
"""


def _run(
    tmp_path: Path, script: str, secrets: str | None = f"CANARY_API_KEY={KEY}\n", **vars_
) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    path = tmp_path / "secrets.env"
    if secrets is not None:
        path.write_text(secrets)
    spec = {
        "module": str(PLUGIN / "hooks" / "register.js"),
        "plugin": str(PLUGIN),
        "secrets": str(path),
        "dir": str(tmp_path),
        "vars": {"key": KEY, "key2": KEY2, **vars_},
        "script": script,
    }
    proc = subprocess.run(
        [node, "-e", _DRIVER, json.dumps(spec)], capture_output=True, text=True, timeout=120
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def test_a_result_is_redacted_and_an_untouched_one_stays_cores_own(tmp_path):
    res = _run(
        tmp_path,
        """
        const out = await t.emit("tool.call", { tool: "Bash" },
          async () => ({ result: { stdout: "key=" + t.v.key, n: 3 }, context: ["again " + t.v.key] }));
        const plain = { result: { stdout: "nothing here" } };
        return { out, identical: (await t.emit("tool.call", { tool: "Bash" }, async () => plain)) === plain };
        """,
    )
    assert res["out"] == {
        "out": {"result": {"stdout": f"key={P}", "n": 3}, "context": [f"again {P}"]},
        "identical": True,
    }


def test_an_errored_call_is_answered_with_its_text_redacted(tmp_path):
    res = _run(
        tmp_path,
        """
        const bad = await t.emit("tool.call", { tool: "Bash" }, async () => ({ isError: true, text: "exit 1: " + t.v.key }));
        const clean = { isError: true, text: "exit 1: nothing" };
        return { bad, identical: (await t.emit("tool.call", { tool: "Bash" }, async () => clean)) === clean };
        """,
    )
    assert res["out"] == {"bad": {"deny": f"exit 1: {P}"}, "identical": True}


def test_a_write_to_the_secrets_file_gets_the_files_own_spelling_back(tmp_path):
    line = 'CANARY_API_KEY="a1b2c3d4e5f60718\\"quoted\\""'
    res = _run(
        tmp_path,
        """
        let seen;
        const next = async e => { seen = e; return { result: "ok" }; };
        const text = 'CANARY_API_KEY="[secrets.env:CANARY_API_KEY]"';
        // Another spelling of the path, which the target check has to resolve.
        await t.emit("tool.call", { tool: "Edit", file_path: t.dir + "/sub/../secrets.env", old_string: text, new_string: text + "\\nNEW=1" }, next);
        const secretsEdit = seen;
        await t.emit("tool.call", { tool: "Write", file_path: t.dir + "/notes.md", content: text }, next);
        return { old: secretsEdit.old_string, neu: secretsEdit.new_string, tool: secretsEdit.tool, elsewhere: seen.content };
        """,
        secrets=f"OTHER=x\n{line}\n",
    )
    assert res["out"] == {
        "old": line,
        "neu": line + "\nNEW=1",
        "tool": "Edit",
        "elsewhere": 'CANARY_API_KEY="[secrets.env:CANARY_API_KEY]"',
    }


def test_a_key_the_tool_adds_or_replaces_is_redacted_in_that_result(tmp_path):
    res = _run(
        tmp_path,
        """
        const added = await t.emit("tool.call", { tool: "Bash" }, async () => {
          t.write(t.read() + "NEW_SERVICE_TOKEN=" + t.v.key2 + "\\n");
          return { result: "wrote " + t.v.key2 };
        });
        // The Edit that replaces a key echoes the old line back: it stays redacted in this session.
        const rotated = await t.emit("tool.call", { tool: "Bash" }, async () => {
          t.write("NEW_SERVICE_TOKEN=" + t.v.key2 + "\\n");
          return { result: "was " + t.v.key };
        });
        return { added, rotated };
        """,
    )
    assert res["out"] == {
        "added": {"result": "wrote [secrets.env:NEW_SERVICE_TOKEN]"},
        "rotated": {"result": f"was {P}"},
    }


def test_placeholder_text_stored_as_a_value_is_reported_and_never_a_needle(tmp_path):
    res = _run(
        tmp_path,
        """
        const out = await t.emit("tool.call", { tool: "Bash" }, async () => ({ result: "a=" + t.v.key + " b=[secrets.env:CANARY_API_KEY]" }));
        t.write(t.read() + "# touched\\n");
        await t.emit("session.start", {});
        return out;
        """,
        secrets=f"CANARY_API_KEY={KEY}\nOTHER_TOKEN=[secrets.env:CANARY_API_KEY]\nPLAIN_URL=x[secrets.env:GONE_KEY]\n",
    )
    # One variable's placeholder is not turned into another's, and the same set is logged once across two loads.
    assert res["out"] == {"result": f"a={P} b={P}"}
    assert [m for m in res["logs"] if "placeholder text" in m] == [
        "genesis-secret-redact: the secrets file holds placeholder text, not a value, for "
        "OTHER_TOKEN, PLAIN_URL; restore those keys from a backup"
    ]


def test_a_failing_loader_is_retried_only_after_the_back_off(tmp_path):
    res = _run(
        tmp_path,
        """
        t.failNext(1);
        await t.emit("session.start", {});
        const raw = await t.emit("tool.call", { tool: "Bash" }, async () => ({ result: t.v.key }));
        await t.emit("session.append", { message: { content: "x" } });
        const during = t.runs();
        t.advance(31000);
        const later = await t.emit("tool.call", { tool: "Bash" }, async () => ({ result: t.v.key }));
        return { raw, during, later, after: t.runs() };
        """,
    )
    assert res["out"] == {"raw": {"result": KEY}, "during": 1, "later": {"result": P}, "after": 2}
    assert len(res["logs"]) == 1 and "this session is NOT redacted" in res["logs"][0]


def test_a_failed_reload_keeps_the_values_loaded_earlier(tmp_path):
    res = _run(
        tmp_path,
        """
        await t.emit("session.start", {});
        t.failNext(1);
        t.write("CANARY_API_KEY=" + t.v.key2 + "\\n");
        return await t.emit("tool.call", { tool: "Bash" }, async () => ({ result: t.v.key }));
        """,
    )
    assert res["out"] == {"result": P}
    assert "keeping the values loaded earlier" in res["logs"][0]


def test_stored_rows_and_prompts_are_redacted(tmp_path):
    res = _run(
        tmp_path,
        """
        const pass = async e => e;
        const row = await t.emit("session.append", { message: { role: "user", content: [{ type: "text", text: "k " + t.v.key }] } }, pass);
        const prompt = await t.emit("prompt.submit", { text: "use " + t.v.key }, pass);
        // The catch handler: a row that could not be scanned keeps its shape, never its text.
        const failed = { message: { content: [{ type: "text", text: "raw" }, { type: "image" }] } };
        const withheld = await t.catchers["session.append"](t.$, failed, Object.assign(pass, { called: false }));
        const delivered = await t.catchers["session.append"](t.$, failed, Object.assign(async e => e, { called: true }));
        return { row: row.message, prompt: prompt.text, withheld: withheld.message.content, delivered: delivered === failed };
        """,
    )
    out = res["out"]
    assert out["row"] == {"role": "user", "content": [{"type": "text", "text": f"k {P}"}]}
    assert out["prompt"] == f"use {P}"
    assert out["withheld"][0]["text"].startswith("[genesis-secret-redact could not redact")
    assert out["withheld"][1] == {"type": "image"} and out["delivered"] is True


def test_each_line_of_a_multiline_value_has_its_own_placeholder_and_restores_alone(tmp_path):
    first, second = "first-line-of-the-secret", "second-line-of-the-secret"
    res = _run(
        tmp_path,
        """
        const shown = await t.emit("tool.call", { tool: "Bash" }, async () => ({ result: "2: " + t.v.second }));
        let seen;
        await t.emit("tool.call", { tool: "Write", file_path: t.secrets, content: "A=[secrets.env:MULTI_SECRET#2]" },
          async e => { seen = e; return { result: "ok" }; });
        return { shown, written: seen.content };
        """,
        secrets=f'MULTI_SECRET="{first}\n{second}"\n',
        second=second,
    )
    assert res["out"] == {
        "shown": {"result": "2: [secrets.env:MULTI_SECRET#2]"},
        "written": f"A={second}",
    }


def test_a_saved_large_output_is_cleaned_and_no_other_file_is(tmp_path):
    res = _run(
        tmp_path,
        """
        const saved = t.dir + "/session/tool-results/out.txt", other = t.dir + "/elsewhere/out.txt";
        t.put(saved, "tail " + t.v.key); t.put(other, "tail " + t.v.key);
        await t.emit("tool.call", { tool: "Bash" }, async () => ({ result: { stdout: "head", persistedOutputPath: saved } }));
        await t.emit("tool.call", { tool: "Bash" }, async () => ({ result: { stdout: "head", persistedOutputPath: other } }));
        return { saved: t.read(saved), other: t.read(other) };
        """,
    )
    assert res["out"] == {"saved": f"tail {P}", "other": f"tail {KEY}"}


def test_no_secrets_file_means_no_redaction_and_no_failure(tmp_path):
    res = _run(
        tmp_path,
        'return await t.emit("tool.call", { tool: "Bash" }, async () => ({ result: t.v.key }));',
        secrets=None,
    )
    assert res == {"out": {"result": KEY}, "logs": []}


def test_only_a_write_tool_aimed_at_the_secrets_file_itself_gets_values(tmp_path):
    (tmp_path / "other.env").write_text("X=1\n")
    res = _run(
        tmp_path,
        """
        const seen = {};
        const write = async (label, tool, file_path) => {
          await t.emit("tool.call", { tool, file_path, content: "[secrets.env:CANARY_API_KEY]", command: "[secrets.env:CANARY_API_KEY]" },
            async e => { seen[label] = e.content; return { result: "ok" }; });
        };
        await write("tilde", "Write", "~/secrets.env");          // Claude Code expands it, so it IS the file
        await write("backup", "Write", t.secrets + ".bak");
        await write("other env", "Write", t.dir + "/other.env");
        await write("trailing space", "Write", t.secrets + " ");
        await write("trailing newline", "Write", t.secrets + "\\n");
        await write("not a write tool", "Bash", t.secrets);
        t.failResolve(1);
        await write("unresolvable", "Write", t.dir + "/sub/../secrets.env");
        return seen;
        """,
    )
    placeholder_kept = {k for k, v in res["out"].items() if v == P}
    assert {k for k, v in res["out"].items() if v == KEY} == {"tilde"}
    assert placeholder_kept == {
        "backup",
        "other env",
        "trailing space",
        "trailing newline",
        "not a write tool",
        "unresolvable",
    }
    assert sum("could not resolve" in m for m in res["logs"]) == 1


def test_a_refusal_that_quotes_restored_input_is_redacted(tmp_path):
    res = _run(
        tmp_path,
        """
        const quoted = await t.emit("tool.call", { tool: "Write", file_path: t.secrets, content: "K=[secrets.env:CANARY_API_KEY]" },
          async e => ({ deny: "refused: " + e.content }));
        const plain = { deny: "refused" };
        return { quoted, identical: (await t.emit("tool.call", { tool: "Bash" }, async () => plain)) === plain };
        """,
    )
    assert res["out"] == {"quoted": {"deny": f"refused: K={P}"}, "identical": True}


def test_an_errored_calls_saved_output_is_cleaned_and_a_link_to_the_secrets_file_is_not_followed(
    tmp_path,
):
    res = _run(
        tmp_path,
        """
        const saved = t.dir + "/s/tool-results/big.txt", link = t.dir + "/s/tool-results/link";
        t.put(saved, "tail " + t.v.key);
        t.link(t.secrets, link);
        await t.emit("tool.call", { tool: "Bash" },
          async () => ({ isError: true, text: "Output too large, saved to: " + saved + " and " + link }));
        return { saved: t.read(saved), secrets: t.read() };
        """,
    )
    assert res["out"] == {"saved": f"tail {P}", "secrets": f"CANARY_API_KEY={KEY}\n"}


def test_a_result_the_walk_cannot_read_is_redacted_through_its_json(tmp_path):
    res = _run(
        tmp_path,
        """
        // A value that throws the first time it is read: the structured walk fails, the JSON pass does not.
        const once = () => { let n = 0; return { enumerable: true, get() { if (n++ === 0) throw new Error("once"); return "v " + t.v.key; } }; };
        const result = Object.defineProperty({}, "out", once());
        const out = await t.emit("tool.call", { tool: "Bash" }, async () => ({ result }));
        const content = [Object.defineProperty({ type: "text" }, "text", once())];
        const row = await t.emit("session.append", { message: { content } }, async e => e);
        return { out, row: row.message.content };
        """,
    )
    assert res["out"] == {
        "out": {"result": {"out": f"v {P}"}},
        "row": [{"type": "text", "text": f"v {P}"}],
    }


def test_cut_off_loader_output_is_a_failed_load_and_each_new_failure_is_logged(tmp_path):
    res = _run(
        tmp_path,
        """
        t.truncateNext(1);
        const raw = await t.emit("tool.call", { tool: "Bash" }, async () => ({ result: t.v.key }));
        t.advance(31000);
        const ok = await t.emit("tool.call", { tool: "Bash" }, async () => ({ result: t.v.key }));
        t.failNext(1);
        t.write("CANARY_API_KEY=" + t.v.key2 + "\\n");
        await t.emit("session.start", {});
        return { raw, ok };
        """,
    )
    assert res["out"] == {"raw": {"result": KEY}, "ok": {"result": P}}
    assert len(res["logs"]) == 2
    assert (
        "more than Claude Code keeps" in res["logs"][0] and "keeping the values" in res["logs"][1]
    )
