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
  let loaderRuns = 0, failLoads = 0, truncate = 0, afterLoader = null, now = Date.now(), tick = 0;
  Date.now = () => now;
  const $ = {
    plugin: { root: spec.plugin },
    ui: { log: m => logs.push(m) },
    fs: {
      stat: async (p, options) => {
        const st = fs.statSync(p);
        const stat = { kind: st.isFile() ? "file" : st.isDirectory() ? "dir" : "other", mtimeMs: st.mtimeMs, size: st.size };
        return options?.resolve ? { ...stat, realPath: fs.realpathSync(p) } : stat;
      },
      read: async p => fs.readFileSync(p, "utf8"),
      write: async (p, text) => fs.writeFileSync(p, text),
    },
    process: { run: argv => new Promise(resolve => {
      loaderRuns += 1;
      if (failLoads > 0) { failLoads -= 1; return resolve({ exitCode: 1, stdout: "", stderr: "" }); }
      // HOME is the scratch directory, so `~/secrets.env` names the scratch secrets file.
      execFile(argv[0], argv.slice(1), { env: { ...process.env, SECRETS_PATH: spec.override ? spec.secrets : "", GENESIS_REPO_ROOT: "", HOME: spec.dir }, encoding: "utf8" },
        (err, stdout, stderr) => {
          const cut = truncate > 0 && (truncate -= 1, true);
          const hook = afterLoader; afterLoader = null;
          if (hook) hook();
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
    remove: p => fs.unlinkSync(p),
    advance: ms => { now += ms; },
    failNext: n => { failLoads = n; },
    afterNextLoader: fn => { afterLoader = fn; },
    truncateNext: n => { truncate = n; },
    link: (target, p) => { fs.mkdirSync(require("path").dirname(p), { recursive: true }); fs.symlinkSync(target, p); },
    runs: () => loaderRuns,
  };
  const out = await new Function("t", `return (async () => { ${spec.script} })()`)(t);
  process.stdout.write(JSON.stringify({ out, logs }));
})().catch(err => { console.error(err); process.exit(1); });
"""


def _run(
    tmp_path: Path,
    script: str,
    secrets: str | None = f"CANARY_API_KEY={KEY}\n",
    plugin: Path = PLUGIN,
    override: bool = True,
    **vars_,
) -> dict:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available")
    path = tmp_path / "secrets.env"
    if secrets is not None:
        path.write_text(secrets)
    spec = {
        "module": str(plugin / "hooks" / "register.js"),
        "plugin": str(plugin),
        "override": override,
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


def test_an_edit_of_the_secrets_file_matches_the_files_own_spelling(tmp_path):
    line = 'CANARY_API_KEY="a1b2c3d4e5f60718\\"quoted\\""'
    res = _run(
        tmp_path,
        """
        let seen;
        const next = async e => { seen = e; return { result: "ok" }; };
        const text = 'CANARY_API_KEY="[secrets.env:CANARY_API_KEY]"';
        // Another spelling of the path, which the target check has to resolve.
        await t.emit("tool.call", { tool: "Edit", file_path: t.dir + "/sub/../secrets.env", old_string: text, new_string: "CANARY_API_KEY=fresh-value-0123456789" }, next);
        const secretsEdit = seen;
        await t.emit("tool.call", { tool: "Write", file_path: t.dir + "/notes.md", content: text }, next);
        return { old: secretsEdit.old_string, neu: secretsEdit.new_string, elsewhere: seen.content };
        """,
        secrets=f"OTHER=x\n{line}\n",
    )
    assert res["out"] == {
        "old": line,
        "neu": "CANARY_API_KEY=fresh-value-0123456789",
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
    # A first load runs the loader twice (once to learn the path, once behind its stamp).
    assert res["out"] == {"raw": {"result": KEY}, "during": 1, "later": {"result": P}, "after": 3}
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
        await t.emit("tool.call", { tool: "Edit", file_path: t.secrets, old_string: "[secrets.env:MULTI_SECRET#2]", new_string: "replacement-line-0123" },
          async e => { seen = e; return { result: "ok" }; });
        return { shown, matched: seen.old_string };
        """,
        secrets=f'MULTI_SECRET="{first}\n{second}"\n',
        second=second,
    )
    assert res["out"] == {
        "shown": {"result": "2: [secrets.env:MULTI_SECRET#2]"},
        "matched": second,
    }


def test_a_saved_large_output_is_cleaned_and_no_other_file_is(tmp_path):
    res = _run(
        tmp_path,
        """
        const saved = t.dir + "/0a1b2c3d-1111-4222-8333-444455556666/tool-results/out.txt", other = t.dir + "/elsewhere/out.txt";
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


def test_only_an_edit_aimed_at_the_secrets_file_itself_gets_values(tmp_path):
    (tmp_path / "other.env").write_text("X=1\n")
    res = _run(
        tmp_path,
        """
        const seen = {};
        const edit = async (label, tool, file_path) => {
          await t.emit("tool.call", { tool, file_path, old_string: "[secrets.env:CANARY_API_KEY]", new_string: "fresh-value-0123456789", command: "x" },
            async e => { seen[label] = e.old_string; return { result: "ok" }; });
        };
        await edit("tilde", "Edit", "~/secrets.env");          // Claude Code expands it, so it IS the file
        await edit("backup", "Edit", t.secrets + ".bak");
        await edit("other env", "Edit", t.dir + "/other.env");
        await edit("trailing space", "Edit", t.secrets + " ");
        await edit("trailing newline", "Edit", t.secrets + "\\n");
        await edit("not a write tool", "Bash", t.secrets);
        await edit("a new file in a new folder", "Edit", t.dir + "/new/deeper/notes.md");
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
        "a new file in a new folder",
    }
    assert res["logs"] == []


def test_a_refusal_that_quotes_restored_input_is_redacted(tmp_path):
    res = _run(
        tmp_path,
        """
        const quoted = await t.emit("tool.call", { tool: "Edit", file_path: t.secrets, old_string: "K=[secrets.env:CANARY_API_KEY]", new_string: "K=fresh-value-0123456789" },
          async e => ({ deny: "refused: " + e.old_string }));
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
        const folder = t.dir + "/my project/0a1b2c3d-1111-4222-8333-444455556666/tool-results/";
        const saved = folder + "big.txt", quoted = folder + "quoted.txt", punct = folder + "punct.txt", link = folder + "link";
        const fixture = t.dir + "/repo/tool-results/fixture.env";
        for (const p of [saved, quoted, punct, fixture]) t.put(p, "tail " + t.v.key);
        t.link(t.secrets, link);
        // Claude Code's own notice ends the path at the end of the line; other tools quote it or punctuate.
        const text = "Output too large. Full output saved to: " + saved + "\\n(see \\"" + quoted + "\\").\\n(kept at " + punct + ").\\nalso " + link + "\\nand " + fixture;
        await t.emit("tool.call", { tool: "Bash" }, async () => ({ isError: true, text }));
        return { saved: t.read(saved), quoted: t.read(quoted), punct: t.read(punct), fixture: t.read(fixture), secrets: t.read() };
        """,
    )
    assert res["out"] == {
        "saved": f"tail {P}",
        "quoted": f"tail {P}",
        "punct": f"tail {P}",
        "fixture": f"tail {KEY}",  # a user's own file under some folder named tool-results
        "secrets": f"CANARY_API_KEY={KEY}\n",
    }
    assert len(res["logs"]) == 2 and all("not found to clean" in m for m in res["logs"])


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


def test_a_write_that_would_store_placeholder_text_is_refused(tmp_path):
    res = _run(
        tmp_path,
        """
        let ran = 0;
        const next = async e => { ran += 1; return { result: "ok" }; };
        const unknown = await t.emit("tool.call", { tool: "Write", file_path: t.secrets, content: "CANARY_API_KEY=[secrets.env:CANARY_API_KEY]\\nOTHER_TOKEN=[secrets.env:NEVER_READ]\\n" }, next);
        const edit = await t.emit("tool.call", { tool: "Edit", file_path: t.secrets, old_string: "x", new_string: "A=[secrets.env:NEVER_READ]" }, next);
        // Even a placeholder this session can restore: what is written never comes from the restore map.
        const known = await t.emit("tool.call", { tool: "Write", file_path: t.secrets, content: "CANARY_API_KEY=[secrets.env:CANARY_API_KEY]\\n" }, next);
        const real = await t.emit("tool.call", { tool: "Write", file_path: t.secrets, content: "CANARY_API_KEY=fresh-value-0123456789\\n" }, next);
        const elsewhere = await t.emit("tool.call", { tool: "Write", file_path: t.dir + "/notes.md", content: "[secrets.env:NEVER_READ]" }, next);
        return { unknown: unknown.deny, edit: edit.deny, known: known.deny, real, elsewhere, ran };
        """,
    )
    out = res["out"]
    assert "[secrets.env:NEVER_READ]" in out["unknown"] and "refused" in out["unknown"]
    assert KEY not in out["unknown"]
    assert "[secrets.env:NEVER_READ]" in out["edit"] and "old_string" in out["known"]
    assert (
        out["real"] == {"result": "ok"} and out["elsewhere"] == {"result": "ok"} and out["ran"] == 2
    )


def test_an_edit_matches_each_line_of_a_file_whose_texts_share_a_value(tmp_path):
    # Two variables hold one value in different quoting, and a comment keeps an encoded copy of it.
    source = (
        'FIRST_API_KEY="abcdefghijklmnop\\"quoted\\""\n'
        "SECOND_API_KEY='abcdefghijklmnop\"quoted\"'\n"
        "# was: abcdefghijklmnop%22quoted%22\n"
    )
    res = _run(
        tmp_path,
        """
        const read = await t.emit("tool.call", { tool: "Read" }, async () => ({ result: t.read() }));
        const matched = [];
        for (const line of read.result.split("\\n").filter(Boolean))
          await t.emit("tool.call", { tool: "Edit", file_path: t.secrets, old_string: line, new_string: "A=fresh-value-0123456789" },
            async e => { matched.push(e.old_string); return { result: "ok" }; });
        return { shown: read.result, matched };
        """,
        secrets=source,
    )
    assert "abcdefghijklmnop" not in res["out"]["shown"]
    assert res["out"]["matched"] == source.splitlines()


def test_a_rotated_out_value_keeps_a_placeholder_of_its_own(tmp_path):
    # The old line is kept as a comment. Both values stay redacted, under different names, and an Edit can name
    # either line.
    res = _run(
        tmp_path,
        """
        await t.emit("session.start", {});
        t.write("# old: CANARY_API_KEY=" + t.v.key + "\\nCANARY_API_KEY=" + t.v.key2 + "\\n");
        const read = await t.emit("tool.call", { tool: "Read" }, async () => ({ result: t.read() }));
        const matched = [];
        for (const line of read.result.split("\\n").filter(Boolean))
          await t.emit("tool.call", { tool: "Edit", file_path: t.secrets, old_string: line, new_string: "A=fresh-value-0123456789" },
            async e => { matched.push(e.old_string); return { result: "ok" }; });
        return { shown: read.result, matched: matched.join("\\n") + "\\n" === t.read() };
        """,
    )
    assert res["out"] == {
        "shown": "# old: CANARY_API_KEY=[secrets.env:CANARY_API_KEY~earlier]\nCANARY_API_KEY=[secrets.env:CANARY_API_KEY]\n",
        "matched": True,
    }


def test_a_change_between_the_first_read_and_its_stamp_is_not_cached_as_current(tmp_path):
    res = _run(
        tmp_path,
        """
        // The file changes right after the first loader run has read it.
        t.afterNextLoader(() => t.write("CANARY_API_KEY=" + t.v.key2 + "\\n"));
        await t.emit("session.start", {});
        return await t.emit("tool.call", { tool: "Bash" }, async () => ({ result: "now " + t.v.key2 }));
        """,
    )
    assert res["out"] == {"result": f"now {P}"}


def test_the_first_event_already_sees_a_change_made_while_the_path_was_learned(tmp_path):
    # The first load reads before the path is known, so nothing stamped that read; it reads again behind a
    # stamp. A prompt is redacted once, with no later reload to catch up, so it shows the difference.
    res = _run(
        tmp_path,
        """
        t.afterNextLoader(() => t.write("CANARY_API_KEY=" + t.v.key2 + "\\n"));
        const prompt = await t.emit("prompt.submit", { text: "use " + t.v.key2 });
        return { prompt, runs: t.runs() };
        """,
    )
    assert res["out"] == {"prompt": {"text": f"use {P}"}, "runs": 2}


def test_a_value_used_as_an_object_key_is_redacted(tmp_path):
    res = _run(
        tmp_path,
        'return await t.emit("tool.call", { tool: "mcp__x__y" }, async () => ({ result: { [t.v.key]: 1, plain: 2 } }));',
    )
    assert res["out"] == {"result": {P: 1, "plain": 2}}


def test_a_held_value_keeps_its_name_through_a_second_rotation(tmp_path):
    # Rotated twice: the first old key keeps the name it was shown under.
    res = _run(
        tmp_path,
        """
        await t.emit("session.start", {});
        t.write("# a: CANARY_API_KEY=" + t.v.key + "\\nCANARY_API_KEY=" + t.v.key2 + "\\n");
        const first = await t.emit("tool.call", { tool: "Read" }, async () => ({ result: t.read() }));
        t.write("# a: CANARY_API_KEY=" + t.v.key + "\\n# b: CANARY_API_KEY=" + t.v.key2 + "\\nCANARY_API_KEY=" + t.v.key3 + "\\n");
        const second = await t.emit("tool.call", { tool: "Read" }, async () => ({ result: t.read() }));
        return { first: first.result, second: second.result };
        """,
        key3="ck_" + "aa11bb22cc33dd44ee55ff6677889900",
    )
    held = "[secrets.env:CANARY_API_KEY~earlier]"
    assert res["out"]["first"].startswith(f"# a: CANARY_API_KEY={held}\n")
    assert res["out"]["second"].startswith(f"# a: CANARY_API_KEY={held}\n")


def test_a_value_equal_to_another_variables_name_is_replaced_once_in_every_hook(tmp_path):
    # One pass, and a placeholder issued by one hook comes out of the next unchanged (a tool result is stored as a
    # row, a placeholder the user types is a prompt).
    res = _run(
        tmp_path,
        """
        await t.emit("session.start", {});
        const shown = await t.emit("tool.call", { tool: "Read" }, async () => ({ result: t.read() }));
        let row, prompt;
        await t.emit("session.append", { message: { content: shown.result } }, async e => { row = e.message.content; return e; });
        await t.emit("prompt.submit", { text: "use " + shown.result }, async e => { prompt = e.text; return e; });
        // A raw value inside placeholder-shaped text this process did not issue is still redacted.
        const shaped = await t.emit("tool.call", { tool: "Bash" }, async () => ({ result: "[secrets.env:" + t.v.key + "]" }));
        return { shown: shown.result, row, prompt, shaped: shaped.result };
        """,
        secrets=f"TOKEN_ENV_NAME=CANARY_SERVICE_TOKEN\nCANARY_SERVICE_TOKEN={KEY}\n",
    )
    # The name on line 2 is TOKEN_ENV_NAME's value, so it is replaced too: once, never inside a placeholder.
    shown = "TOKEN_ENV_NAME=[secrets.env:TOKEN_ENV_NAME]\n[secrets.env:TOKEN_ENV_NAME]=[secrets.env:CANARY_SERVICE_TOKEN]\n"
    assert res["out"] == {
        "shown": shown,
        "row": shown,
        "prompt": "use " + shown,
        "shaped": "[secrets.env:[secrets.env:CANARY_SERVICE_TOKEN]]",
    }


def test_a_write_with_placeholder_text_is_refused_when_the_hook_failed_or_nothing_loaded(tmp_path):
    res = _run(
        tmp_path,
        """
        let ran = 0;
        const next = async e => { ran += 1; return { result: "ok" }; };
        // No load has succeeded yet: nothing can be restored, so the write is refused.
        t.failNext(2);
        const unloaded = await t.emit("tool.call", { tool: "Write", file_path: t.secrets, content: "CANARY_API_KEY=[secrets.env:CANARY_API_KEY]\\n" }, next);
        // SECRETS_PATH may name any file, so before a load any target is refused, not only one named secrets.env.
        const custom = await t.emit("tool.call", { tool: "Write", file_path: t.dir + "/credentials.env", content: "K=[secrets.env:CANARY_API_KEY]\\n" }, next);
        // The hook failed before the tool ran.
        const failed = Object.assign(async e => { ran += 1; return { result: "ok" }; }, { called: false, error: { kind: "error", message: "x" } });
        const caught = await t.catchers["tool.call"](t.$, { tool: "Edit", file_path: t.dir + "/x", old_string: "a", new_string: "[secrets.env:CANARY_API_KEY]" }, failed);
        const plain = await t.catchers["tool.call"](t.$, { tool: "Edit", file_path: t.dir + "/x", old_string: "a", new_string: "b" }, failed);
        return { unloaded: unloaded.deny, custom: custom.deny, caught: caught.deny, plain, ran };
        """,
    )
    out = res["out"]
    assert (
        "refused" in out["unloaded"] and "refused" in out["custom"] and "refused" in out["caught"]
    )
    assert out["plain"] == {"result": "ok"} and out["ran"] == 1


def test_a_rotation_or_a_resume_can_never_write_the_new_key_into_an_old_line(tmp_path):
    # Read, then the key rotates in a different quoting. Writing the earlier read back is refused; an Edit naming
    # the old line no longer matches the file, so the tool changes nothing. A second process (a resumed session)
    # that never saw the old value behaves the same.
    script = """
        const shown = await t.emit("tool.call", { tool: "Read" }, async () => ({ result: t.read() }));
        t.write("CANARY_API_KEY='" + t.v.key2 + "\\"quoted\\"'\\n");
        const tool = async e => (e.old_string === undefined || t.read().includes(e.old_string) ? { result: "ok" } : { isError: true, text: "old_string not found" });
        const write = await t.emit("tool.call", { tool: "Write", file_path: t.secrets, content: shown.result + "NEW=1\\n" }, tool);
        const edit = await t.emit("tool.call", { tool: "Edit", file_path: t.secrets, old_string: shown.result.trim(), new_string: "NEW=1" }, tool);
        return { shown: shown.result, write: write.deny !== undefined, edit: edit.isError === true || edit.deny !== undefined, file: t.read() };
        """
    res = _run(tmp_path, script, secrets=f'CANARY_API_KEY="{KEY}"\n')
    assert res["out"]["shown"] == f'CANARY_API_KEY="{P}"\n'
    assert res["out"]["write"] is True and res["out"]["edit"] is True
    assert res["out"]["file"] == f"CANARY_API_KEY='{KEY2}\"quoted\"'\n"


def test_a_key_is_rotated_in_place_with_an_edit_and_a_line_next_to_a_secret_is_refused(tmp_path):
    res = _run(
        tmp_path,
        """
        const shown = await t.emit("tool.call", { tool: "Read" }, async () => ({ result: t.read() }));
        let seen;
        const tool = async e => { seen = e; return { result: "ok" }; };
        await t.emit("tool.call", { tool: "Edit", file_path: t.secrets, old_string: shown.result.trim(), new_string: "CANARY_API_KEY=" + t.v.key2 }, tool);
        const rotated = { old: seen.old_string, neu: seen.new_string };
        const append = await t.emit("tool.call", { tool: "Edit", file_path: t.secrets, old_string: shown.result.trim(), new_string: shown.result.trim() + "\\nNEW=1" }, tool);
        return { rotated, append: append.deny };
        """,
    )
    assert res["out"]["rotated"] == {
        "old": f"CANARY_API_KEY={KEY}",
        "neu": f"CANARY_API_KEY={KEY2}",
    }
    assert "refused" in res["out"]["append"] and "anchor" in res["out"]["append"]


def test_a_secrets_file_the_resolver_would_now_prefer_is_loaded_on_the_next_event(tmp_path):
    # No SECRETS_PATH: the resolver prefers ~/.config/genesis/secrets.env when it exists. Creating it mid-session
    # must be noticed although the file loaded first is unchanged.
    repo = tmp_path / "repo"
    shutil.copytree(PLUGIN, repo / "plugins" / "genesis-secret-redact")
    resolver = repo / "src" / "genesis" / "secrets_location.py"
    resolver.parent.mkdir(parents=True)
    shutil.copy(PLUGIN.parents[1] / "src" / "genesis" / "secrets_location.py", resolver)
    (tmp_path / "genesis").mkdir()
    (tmp_path / "genesis" / "secrets.env").write_text(f"CANARY_API_KEY={KEY}\n")
    res = _run(
        tmp_path,
        """
        const before = await t.emit("tool.call", { tool: "Bash" }, async () => ({ result: t.v.key }));
        t.put(t.dir + "/.config/genesis/secrets.env", "CANARY_API_KEY=" + t.v.key2 + "\\n");
        const after = await t.emit("tool.call", { tool: "Bash" }, async () => ({ result: t.v.key2 }));
        // Nothing changes now, so the next event runs no loader.
        const runs = t.runs();
        await t.emit("tool.call", { tool: "Bash" }, async () => ({ result: "x" }));
        return { before, after, idle: t.runs() === runs };
        """,
        secrets=None,
        plugin=repo / "plugins" / "genesis-secret-redact",
        override=False,
    )
    assert res["out"] == {"before": {"result": P}, "after": {"result": P}, "idle": True}


def test_a_write_with_placeholder_text_is_refused_while_a_failed_reload_leaves_the_file_unconfirmed(
    tmp_path,
):
    # The resolver moves to a file created mid-session and the reload that would learn it fails: the mod still
    # knows only the old path, so a write to the new one carrying placeholder text must be refused anyway.
    repo = tmp_path / "repo"
    shutil.copytree(PLUGIN, repo / "plugins" / "genesis-secret-redact")
    resolver = repo / "src" / "genesis" / "secrets_location.py"
    resolver.parent.mkdir(parents=True)
    shutil.copy(PLUGIN.parents[1] / "src" / "genesis" / "secrets_location.py", resolver)
    (tmp_path / "genesis").mkdir()
    (tmp_path / "genesis" / "secrets.env").write_text(f"CANARY_API_KEY={KEY}\n")
    res = _run(
        tmp_path,
        """
        await t.emit("tool.call", { tool: "Bash" }, async () => ({ result: "loaded" }));
        const moved = t.dir + "/.config/genesis/secrets.env";
        t.put(moved, "CANARY_API_KEY=" + t.v.key2 + "\\n");
        t.failNext(1);
        let ran = 0;
        const run = async () => { ran += 1; return { result: "ok" }; };
        const write = file_path => ({ tool: "Write", file_path, content: "CANARY_API_KEY=[secrets.env:CANARY_API_KEY]\\n" });
        const failed = await t.emit("tool.call", write(moved), run);
        const plain = await t.emit("tool.call", { tool: "Write", file_path: moved, content: "x" }, run);
        // Still inside the back-off: no reload runs, and the target is still unconfirmed.
        t.advance(5000);
        const again = await t.emit("tool.call", write(moved), run);
        const ranWhileUnconfirmed = ran;
        t.advance(31000);
        await t.emit("tool.call", { tool: "Bash" }, async () => ({ result: "reloaded" }));
        const elsewhere = await t.emit("tool.call", write(t.dir + "/notes.md"), run);
        const target = await t.emit("tool.call", write(moved), run);
        return { failed: failed.deny.includes("failed at the last reload"), again: "deny" in again, plain: plain.result, ranWhileUnconfirmed, elsewhere: elsewhere.result, target: "deny" in target, ran };
        """,
        secrets=None,
        plugin=repo / "plugins" / "genesis-secret-redact",
        override=False,
    )
    # Refused while unconfirmed; a write without placeholders still runs; once a load succeeds the ordinary
    # rule applies: placeholders elsewhere stay literal, the secrets file (now the moved one) still refuses.
    assert res["out"] == {
        "failed": True,
        "again": True,
        "plain": "ok",
        "ranWhileUnconfirmed": 1,
        "elsewhere": "ok",
        "target": True,
        "ran": 2,
    }


def test_an_unconfirmed_file_is_retried_after_the_back_off_even_when_every_stamp_is_back(tmp_path):
    # A newly preferred secrets file appears, the reload it causes fails, and the file is removed during the
    # back-off: every watched path is back to the stamp of the last successful load. The reload must still be
    # retried once the back-off passes, or placeholder text in a write to any file stays refused all session.
    repo = tmp_path / "repo"
    shutil.copytree(PLUGIN, repo / "plugins" / "genesis-secret-redact")
    resolver = repo / "src" / "genesis" / "secrets_location.py"
    resolver.parent.mkdir(parents=True)
    shutil.copy(PLUGIN.parents[1] / "src" / "genesis" / "secrets_location.py", resolver)
    (tmp_path / "genesis").mkdir()
    (tmp_path / "genesis" / "secrets.env").write_text(f"CANARY_API_KEY={KEY}\n")
    res = _run(
        tmp_path,
        """
        await t.emit("tool.call", { tool: "Bash" }, async () => ({ result: "loaded" }));
        const moved = t.dir + "/.config/genesis/secrets.env";
        t.put(moved, "CANARY_API_KEY=" + t.v.key2 + "\\n");
        t.failNext(1);
        let ran = 0;
        const run = async () => { ran += 1; return { result: "ok" }; };
        const notes = { tool: "Write", file_path: t.dir + "/notes.md", content: "see [secrets.env:CANARY_API_KEY]\\n" };
        const failed = await t.emit("tool.call", notes, run);
        t.remove(moved);
        t.advance(31000);
        const runs = t.runs();
        const recovered = await t.emit("tool.call", notes, run);
        const retried = t.runs() > runs;
        // Confirmed again, so nothing changes and the next event runs no loader.
        const idleRuns = t.runs();
        await t.emit("tool.call", { tool: "Bash" }, async () => ({ result: "x" }));
        return { failed: "deny" in failed, recovered: recovered.result, retried, idle: t.runs() === idleRuns, ran };
        """,
        secrets=None,
        plugin=repo / "plugins" / "genesis-secret-redact",
        override=False,
    )
    assert res["out"] == {
        "failed": True,
        "recovered": "ok",
        "retried": True,
        "idle": True,
        "ran": 1,
    }
