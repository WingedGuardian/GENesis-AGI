#!/usr/bin/env python3
"""End-to-end check of the genesis-secret-redact plugin against a real Claude Code, with canary values only.

Builds a scratch secrets file (selected through SECRETS_PATH, which the runtime's resolver honours), runs one
non-interactive Claude Code session with the plugin loaded from a directory (default: this checkout's copy),
and asks it to read the canary through every path the plugin covers and to write to the secrets file with
Write (expected to be refused when it carries placeholders) and with Edit. Then it checks, step by step, that each scripted call ran and came back holding the
placeholder (a leak scan proves nothing about a step that was skipped), that no canary value reached the model's final
answer or any record Claude Code kept for the session (the transcript, subagent transcripts, persisted large
outputs, background task output files), and that the secrets file came out holding its values, byte for byte
where the session did not change them. A PreToolUse logging hook beside the plugin records the command each
sibling hook saw, so the run also proves the stream wrap left other guards reading the original command. The typed-prompt path is not scripted here: a `-p` run keeps the engine's own record of its prompt, which
no plugin can rewrite.

Needs Claude Code on PATH and a logged-in account; it spends one short session. Not run in CI. Never touches a
real secret: the canary values are generated per run and the real secrets file is not read.

    scripts/e2e_secret_redact.py [--plugin-dir DIR] [--model MODEL] [--keep]
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

PROMPT = """This is a scripted redaction test in a scratch directory. Do every step, in order, with exactly the
tool named, and do not skip any:
1. Bash: cat app.conf
2. Bash: env | grep CANARY_ENV
3. Bash: seq 1 30000 && cat app.conf   (if the output is saved to a file, Read its last 5 lines)
4. Read tool: app.conf
5. Agent tool (general-purpose): 'Run Bash: cat app.conf and reply with its output verbatim'. Quote the reply.
5b. Monitor tool: command 'cat app.conf; exit 3'. Wait for it to end.
5c. Bash with run_in_background=true: 'cat app.conf; (sleep 2; echo late-line; cat app.conf) & exit 4'.
   Wait for its completion notification, run Bash 'sleep 5', then Read its output file.
6. If an MCP tool named mcp__canary__canary_value is available, call it and quote its result. If one named
   mcp__canary__canary_fail is available, call it too (it reports an error) and quote the error.
7. Write tool: create notes2.md containing exactly the text you saw for the key in step 4.
8. Bash: cat app.conf; exit 1   (it exits non-zero on purpose; quote its output)
8b. Write tool: write the file {secrets} with exactly this one line: OTHER_TOKEN=[secrets.env:NEVER_READ]
   (the plugin is expected to refuse this write; quote the refusal and go on)
9. Read the file {secrets} with the Read tool. Then use the Write tool to write that same file back with
   every line exactly as you read it, plus one more line at the end: CANARY_ADDED=added-value-1
   (the plugin is expected to refuse this write; quote the refusal and go on)
9c. Use the Edit tool on {secrets} to replace the line CANARY_REGION=test-region-1 with two lines:
   CANARY_REGION=test-region-1 and then CANARY_ADDED=added-value-1
10. LAST: Read {secrets} again, then use the Edit tool on it to replace the whole CANARY_API_KEY line exactly
   as you read it with the line CANARY_API_KEY_ROTATED=rotated-value-000000001 .
(Use that path exactly as written, with its "..", every time.)
Finally quote, verbatim, every line containing "key=" or "CANARY" that you received, naming its step."""

#: A two-tool MCP server that returns the canary file, standing in for an MCP tool (such as a reference lookup)
#: that hands a stored value back to the model.
MCP_SERVER = """from fastmcp import FastMCP
mcp = FastMCP("canary")

@mcp.tool()
def canary_value() -> str:
    \"\"\"Return the canary config file.\"\"\"
    return open({conf!r}).read()

@mcp.tool()
def canary_fail() -> str:
    \"\"\"Fail with the canary config file in the error message.\"\"\"
    raise ValueError("lookup failed for: " + open({conf!r}).read())

if __name__ == "__main__":
    mcp.run()
"""


def _calls(transcript: Path) -> list[dict]:
    """Every tool call of a session transcript that got a result: its tool, its input (as JSON text), the
    result's text, whether the result is an error, and the transcript line that holds the result."""
    uses: dict[str, dict] = {}
    calls: list[dict] = []
    for line in transcript.read_text(errors="replace").splitlines():
        try:
            content = json.loads(line).get("message", {}).get("content")
        except (json.JSONDecodeError, AttributeError):
            continue
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use":
                uses[block.get("id", "")] = block
            elif block.get("type") == "tool_result" and block.get("tool_use_id") in uses:
                use = uses[block["tool_use_id"]]
                body = block.get("content")
                if isinstance(body, list):
                    body = "".join(b.get("text", "") for b in body if isinstance(b, dict))
                calls.append(
                    {
                        "tool": use.get("name", ""),
                        "input": json.dumps(use.get("input", {})),
                        "text": body or "",
                        "error": bool(block.get("is_error")),
                        "record": line,
                    }
                )
    return calls


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin-dir", default=str(REPO / "plugins" / "genesis-secret-redact"))
    parser.add_argument("--model", default="claude-haiku-5-5")
    parser.add_argument("--keep", action="store_true", help="keep the scratch directory")
    args = parser.parse_args()
    if shutil.which("claude") is None:
        print("SKIP: claude is not on PATH")
        return 2

    scratch_parent = Path.home() / "tmp"
    scratch_parent.mkdir(exist_ok=True)
    base = Path(tempfile.mkdtemp(prefix="secret-redact-e2e-", dir=scratch_parent))
    proj = base / "proj"
    proj.mkdir()
    key = "ck_" + secrets.token_hex(16)
    token = "ct_" + secrets.token_urlsafe(24)
    # A value the file has to store with escapes: restoring it as parsed would corrupt the line.
    quoted_head = "cq_" + secrets.token_hex(8)
    quoted_line = f'CANARY_QUOTED_SECRET="{quoted_head}\\"mid\\"{secrets.token_hex(4)}"'
    secrets_file = base / "secrets.env"
    secrets_file.write_text(
        f'CANARY_API_KEY={key}\nCANARY_SERVICE_TOKEN="{token}"\n{quoted_line}\nCANARY_REGION=test-region-1\n'
    )
    # The session reaches the secrets file through another spelling of its path, which the restore check must
    # resolve. (A write through a symlink is refused before the plugin sees it: measured on 2.1.299.)
    secrets_link = proj / ".." / "secrets.env"
    secrets_file.chmod(0o600)
    (proj / "app.conf").write_text(f"config line\nkey={key}\n")
    guard_log = base / "guard.log"
    logger = base / "guard_log.py"
    logger.write_text(
        "import json,sys\n"
        "d=json.load(sys.stdin)\n"
        f"open({str(guard_log)!r},'a').write(json.dumps({{'tool':d.get('tool_name'),"
        "'cmd':(d.get('tool_input') or {}).get('command','')})+'\\n')\n"
    )
    settings = base / "settings.json"
    hook = {
        "matcher": "Bash|Monitor",
        "hooks": [{"type": "command", "command": f"python3 {logger}"}],
    }
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [hook]}}))

    mcp_args: list[str] = []
    # A linked worktree has no venv of its own; the install's checkout does.
    install_root = Path(os.environ.get("GENESIS_REPO_ROOT") or Path.home() / "genesis")
    venv_python = next(
        (p for p in (REPO / ".venv/bin/python", install_root / ".venv/bin/python") if p.exists()),
        None,
    )
    if venv_python is not None:
        server = base / "canary_mcp.py"
        server.write_text(MCP_SERVER.format(conf=str(proj / "app.conf")))
        mcp_config = base / "mcp.json"
        mcp_config.write_text(
            json.dumps(
                {"mcpServers": {"canary": {"command": str(venv_python), "args": [str(server)]}}}
            )
        )
        mcp_args = ["--mcp-config", str(mcp_config)]
    else:
        print("NOTE: no repo venv, so the MCP path is not exercised")

    env = {**os.environ, "SECRETS_PATH": str(secrets_file), "CANARY_ENV_VALUE": token}
    run = subprocess.run(
        [
            "claude",
            "-p",
            "--dangerously-skip-permissions",
            "--model",
            args.model,
            "--plugin-dir",
            args.plugin_dir,
            "--settings",
            str(settings),
            *mcp_args,
            "--output-format",
            "json",
            PROMPT.format(secrets=secrets_link),
        ],
        cwd=proj,
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    if run.returncode != 0:
        print(f"FAIL: claude exited {run.returncode}: {run.stderr[-400:]}")
        return 1
    out = json.loads(run.stdout)
    sid = out["session_id"]
    # Any spelling of the quoted value contains its head, so the head stands for it in the leak scan.
    values = [key, token, quoted_head]

    def raw_in(text: str) -> int:
        return sum(text.count(v) for v in values)

    checks: list[tuple[str, bool, str]] = []
    checks.append(("final answer has no canary", raw_in(out.get("result", "")) == 0, ""))
    # Find the session's records by its id, not by re-deriving Claude Code's folder naming.
    found = list((Path.home() / ".claude" / "projects").glob(f"*/{sid}.jsonl"))
    if not found:
        print(f"FAIL: no transcript found for session {sid}")
        return 1
    transcripts = found[0].parent
    transcript = transcripts / f"{sid}.jsonl"

    # The leak scan below proves nothing about a step the session skipped, so first: each scripted call ran and
    # its result came back holding the placeholder.
    calls = _calls(transcript)
    api_key, service_token = "[secrets.env:CANARY_API_KEY]", "[secrets.env:CANARY_SERVICE_TOKEN]"

    def ran(tools: tuple[str, ...], in_input: str) -> list[dict]:
        return [c for c in calls if c["tool"] in tools and in_input in c["input"]]

    def step(
        label: str, tools: tuple[str, ...], in_input: str, holds: str, error: bool = False
    ) -> None:
        hits = ran(tools, in_input)
        ok = any(holds in c["text"] and c["error"] == error for c in hits)
        checks.append((label, ok, f"{len(hits)} calls"))

    step("step 1: cat showed the placeholder", ("Bash",), '"cat app.conf"', api_key)
    step("step 2: env showed the placeholder", ("Bash",), "CANARY_ENV", service_token)
    step("step 3: the large output ran", ("Bash",), "seq 1 30000", "")
    step("step 4: Read showed the placeholder", ("Read",), "app.conf", api_key)
    # The Agent tool answers at once and delivers the reply later, so its proof is the subagent's own transcript
    # (checked below), which holds what its Bash call returned.
    step("step 5: a subagent was started", ("Agent", "Task"), "app.conf", "")
    if mcp_args:
        step(
            "step 6: the MCP result showed the placeholder",
            ("mcp__canary__canary_value",),
            "",
            api_key,
        )
        step(
            "step 6: the MCP error showed the placeholder",
            ("mcp__canary__canary_fail",),
            "",
            api_key,
            error=True,
        )
    step(
        "step 8: the failed command's output showed the placeholder",
        ("Bash",),
        "exit 1",
        api_key,
        error=True,
    )
    step(
        "step 9: Read of the secrets file showed placeholders",
        ("Read",),
        "secrets.env",
        "[secrets.env:CANARY_QUOTED_SECRET]",
    )
    step(
        "step 8b: a write that would store placeholder text was refused",
        ("Write",),
        "NEVER_READ",
        "refused",
        error=True,
    )
    step(
        "step 9: a whole-file Write holding placeholders was refused",
        ("Write",),
        "CANARY_ADDED",
        "refused",
        error=True,
    )
    step(
        "step 9c: the Edit anchored on a line without a secret ran", ("Edit",), "CANARY_REGION", ""
    )
    step("step 5b: the Monitor command ran", ("Monitor",), "cat app.conf", "")
    step("step 5c: the background command ran", ("Bash",), "run_in_background", "")
    edits = ran(("Edit",), "secrets.env")
    checks.append(
        (
            "step 10: the Edit ran, and its result holds neither the old key nor the new one",
            any(
                not c["error"]
                and key not in c["record"]
                and "rotated-value-000000001" not in c["record"]
                for c in edits
            ),
            f"{len(edits)} calls",
        )
    )
    for label, pattern in (
        ("the saved large output", "tool-results/*"),
        ("the subagent's transcript", "subagents/*.jsonl"),
    ):
        held = [
            p
            for p in (transcripts / sid).rglob(pattern)
            if api_key in p.read_text(errors="replace")
        ]
        checks.append((f"{label} holds the placeholder", bool(held), f"{len(held)} files"))

    # Claude Code keeps task output under CLAUDE_CODE_TMPDIR when that is set, else under TMPDIR (measured).
    tmp_base = (
        os.environ.get("CLAUDE_CODE_TMPDIR") or os.environ.get("TMPDIR") or tempfile.gettempdir()
    )
    tmp_root = Path(tmp_base) / f"claude-{os.getuid()}"
    tasks = tmp_root / transcripts.name / sid / "tasks"
    task_files = sorted(tasks.glob("*.output")) if tasks.is_dir() else []
    task_text = "".join(p.read_text(errors="replace") for p in task_files)
    # Steps 5b and 5c each cat app.conf into a task file of its own: each must hold the placeholder by itself.
    conf_files = [p for p in task_files if "config line" in p.read_text(errors="replace")]
    seen = (
        [json.loads(line) for line in guard_log.read_text().splitlines()]
        if guard_log.exists()
        else []
    )
    checks += [
        ("background task output files found", bool(task_files), str(tasks)),
        (
            "the Monitor's and the background command's task files each hold the placeholder",
            len(conf_files) >= 2
            and all(api_key in p.read_text(errors="replace") for p in conf_files),
            f"{len(conf_files)} files",
        ),
        ("late background output reached the task file", "late-line" in task_text, ""),
        (
            "no runner internals or job-control notices in task output",
            "_sr_" not in task_text
            and "]+  " not in task_text
            and "redact_run.sh" not in task_text,
            "",
        ),
        (
            "sibling guards saw the Monitor and background commands as written, never the runner",
            any(s["tool"] == "Monitor" and "cat app.conf; exit 3" in s["cmd"] for s in seen)
            and any(s["tool"] == "Bash" and "late-line" in s["cmd"] for s in seen)
            and not any("redact_run.sh" in s["cmd"] for s in seen),
            f"{len(seen)} commands logged",
        ),
    ]
    stores = [transcript] + sorted((transcripts / sid).rglob("*")) + task_files
    for p in stores:
        if p.is_file():
            n = raw_in(p.read_text(errors="replace"))
            checks.append(
                (
                    f"no canary in {p.relative_to(p.parents[2]) if len(p.parents) > 2 else p}",
                    n == 0,
                    f"{n} raw",
                )
            )
    lines = secrets_file.read_text().splitlines()
    notes2 = proj / "notes2.md"
    checks += [
        (
            "a placeholder written to another file stays literal",
            notes2.exists() and api_key in notes2.read_text() and raw_in(notes2.read_text()) == 0,
            "notes2.md missing" if not notes2.exists() else "",
        ),
        ("the token's line is unchanged", f'CANARY_SERVICE_TOKEN="{token}"' in lines, ""),
        (
            "the refused Write left the escaped value byte for byte",
            quoted_line in lines,
            "",
        ),
        ("the line the Edit added is there", "CANARY_ADDED=added-value-1" in lines, ""),
        ("the Edit applied", "CANARY_API_KEY_ROTATED=rotated-value-000000001" in lines, ""),
        (
            "the secrets file holds no placeholder",
            "[secrets.env:" not in "\n".join(lines),
            "",
        ),
    ]

    # Default permission mode: the wrap must not touch the command, or Claude Code's permission check judges the
    # wrapper (`source ...`) and refuses an allow-listed background command (measured before the bypass gate).
    allow = base / "allow-settings.json"
    allow.write_text(json.dumps({"permissions": {"allow": ["Bash(echo DEFAULTMODE-RAN)"]}}))
    plain = subprocess.run(
        [
            "claude",
            "-p",
            "--permission-mode",
            "default",
            "--model",
            args.model,
            "--plugin-dir",
            args.plugin_dir,
            "--settings",
            str(allow),
            "--output-format",
            "json",
            "Use the Bash tool once with run_in_background set to true and command: echo DEFAULTMODE-RAN"
            " . Wait for it to finish, then reply with the word DONE.",
        ],
        cwd=proj,
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
    )
    try:
        denials = json.loads(plain.stdout).get("permission_denials") or []
    except json.JSONDecodeError:
        denials = None
    checks.append(
        (
            "default mode: an allow-listed background command ran (the wrap left it alone)",
            plain.returncode == 0 and denials == [],
            f"exit {plain.returncode}, denials {denials}",
        )
    )

    failed = [c for c in checks if not c[1]]
    for name, ok, note in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name}{('  (' + note + ')') if note else ''}")
    print(
        f"\n{len(checks) - len(failed)}/{len(checks)} checks passed; session {sid}; scratch {base}"
    )
    if not args.keep and not failed:
        shutil.rmtree(base, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
