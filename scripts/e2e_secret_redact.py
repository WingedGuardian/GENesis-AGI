#!/usr/bin/env python3
"""End-to-end check of the genesis-secret-redact plugin against a real Claude Code, with canary values only.

Builds a scratch secrets file (selected through SECRETS_PATH, which the runtime's resolver honours), runs one
non-interactive Claude Code session with the plugin loaded from a directory (default: this checkout's copy),
and asks it to read the canary through every path the plugin covers. Then it checks, for each path, that no
canary value reached the model's final answer or any record Claude Code kept for the session: the transcript,
subagent transcripts, persisted large outputs, background task output files and the prompt history. A
PreToolUse logging hook beside the plugin records the command each sibling hook saw, so the run also proves
the stream wrap left other guards reading the original command.

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
5. Monitor tool: command 'cat app.conf; exit 3'. Wait for it to end.
6. Bash with run_in_background=true: 'cat app.conf; (sleep 2; echo late-line; cat app.conf) & exit 4'.
   Wait for its completion notification, run Bash 'sleep 5', then Read its output file.
7. Agent tool (general-purpose): 'Run Bash: cat app.conf and reply with its output verbatim'. Quote the reply.
8. If an MCP tool named mcp__canary__canary_value is available, call it and quote its result. If one named
   mcp__canary__canary_fail is available, call it too (it reports an error) and quote the error.
9. Write tool: create notes2.md containing exactly the text you saw for the key in step 4.
9b. Bash: cat app.conf; exit 1   (it exits non-zero on purpose; quote its output)
10. LAST: Read the file {secrets} with the Read tool, then use the Edit tool on that file to replace the whole
   CANARY_API_KEY line exactly as you read it with the line CANARY_API_KEY_ROTATED=rotated-value-000000001 .
   (Use that path exactly as written, with its "..", for both the Read and the Edit.)
Finally quote, verbatim, every line containing "key=" or "CANARY" that you received, naming its step.
(Step 10 comes last on purpose: once the key is rotated out of the secrets file it is no longer one of this
install's values, so the plugin stops redacting it.)"""

#: A one-tool MCP server that returns the canary file, standing in for an MCP tool (such as a reference lookup)
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


def _canaries() -> tuple[str, str]:
    return "ck_" + secrets.token_hex(16), "ct_" + secrets.token_urlsafe(24)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plugin-dir", default=str(REPO / "plugins" / "genesis-secret-redact"))
    parser.add_argument("--model", default="claude-haiku-5-5")
    parser.add_argument("--keep", action="store_true", help="keep the scratch directory")
    args = parser.parse_args()
    if shutil.which("claude") is None:
        print("SKIP: claude is not on PATH")
        return 2

    base = Path(tempfile.mkdtemp(prefix="secret-redact-e2e-", dir=Path.home() / "tmp"))
    proj = base / "proj"
    proj.mkdir()
    key, token = _canaries()
    secrets_file = base / "secrets.env"
    secrets_file.write_text(f'CANARY_API_KEY={key}\nCANARY_SERVICE_TOKEN="{token}"\n')
    # The session edits the secrets file through another spelling of its path, which the restore check must
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
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash|Monitor",
                            "hooks": [{"type": "command", "command": f"python3 {logger}"}],
                        }
                    ]
                }
            }
        )
    )

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

    history = Path.home() / ".claude" / "history.jsonl"
    history_mark = history.stat().st_size if history.exists() else 0
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
    values = [key, token]

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
    tmp_root = Path(os.environ.get("TMPDIR") or tempfile.gettempdir()) / f"claude-{os.getuid()}"
    task_dirs = [d for d in [tmp_root / transcripts.name / sid / "tasks"] if d.is_dir()]
    checks.append(
        (
            "background task output dir found",
            bool(task_dirs),
            str(tmp_root / transcripts.name / sid),
        )
    )
    stores = [transcripts / f"{sid}.jsonl"] + sorted((transcripts / sid).rglob("*"))
    for d in task_dirs:
        stores += sorted(d.glob("*"))
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
    if history.exists():
        with history.open("rb") as fh:
            fh.seek(history_mark)
            n = raw_in(fh.read().decode(errors="replace"))
        checks.append(("no canary in prompt history (new lines)", n == 0, f"{n} raw"))
    edited = secrets_file.read_text()
    checks.append(
        (
            "Edit of the secrets file restored placeholders (file holds no placeholder)",
            "[secrets.env:" not in edited,
            "",
        )
    )
    checks.append(
        (
            "Edit of the secrets file applied",
            "CANARY_API_KEY_ROTATED=rotated-value-000000001" in edited,
            "",
        )
    )
    notes2 = proj / "notes2.md"
    checks.append(
        (
            "a placeholder written to another file stays literal",
            notes2.exists() and raw_in(notes2.read_text()) == 0,
            "notes2.md missing" if not notes2.exists() else "",
        )
    )
    seen = (
        [json.loads(line) for line in guard_log.read_text().splitlines()]
        if guard_log.exists()
        else []
    )
    checks.append(
        (
            "sibling guards saw original commands (none saw the runner)",
            bool(seen) and not any("redact_run.sh" in s["cmd"] for s in seen),
            f"{len(seen)} commands logged",
        )
    )
    checks.append(
        (
            "late background output reached the task file",
            any(
                "late-line" in p.read_text(errors="replace")
                for d in task_dirs
                for p in d.glob("*.output")
            ),
            "",
        )
    )
    task_text = "".join(
        p.read_text(errors="replace") for d in task_dirs for p in d.glob("*.output")
    )
    checks.append(
        (
            "no runner internals or job-control notices in task output",
            "_sr_" not in task_text
            and "]+  " not in task_text
            and "redact_run.sh" not in task_text,
            "",
        )
    )
    if mcp_args:
        transcript_text = (transcripts / f"{sid}.jsonl").read_text(errors="replace")
        checks.append(
            (
                "the MCP tool was called (its result is covered by the transcript check)",
                "mcp__canary__canary_value" in transcript_text,
                "",
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
