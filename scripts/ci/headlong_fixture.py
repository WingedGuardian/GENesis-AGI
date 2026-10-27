#!/usr/bin/env python3
"""Synthetic text providers. Never invoke a real coding agent or provider."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


def response(kind):
    if kind == "failure":
        raise ValueError("synthetic provider failure")
    code = "printf 'synthetic-effect\\n' > effect.txt\nFINAL='done'" if kind == "effect" else "FINAL='no-op'"
    return f"```bash\n{code}\n```\n"


def capture(row):
    with Path(os.environ["CONTROL_CAPTURE"]).open("a") as stream:
        stream.write(json.dumps(row) + "\n")


def cli_records(text):
    return [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}},
        {"type": "result", "subtype": "success", "is_error": False, "result": text},
    ]


def cli_text(output):
    rows = [json.loads(line) for line in output.splitlines()]
    if len(rows) != 2 or rows[0].get("type") != "assistant":
        raise ValueError("missing text-only assistant result")
    blocks = rows[0]["message"]["content"]
    if not blocks or any(block.get("type") != "text" for block in blocks):
        raise ValueError("non-text assistant response")
    text = "".join(block["text"] for block in blocks)
    result = rows[1]
    if (result.get("type"), result.get("subtype"), result.get("is_error"), result.get("result")) != (
        "result", "success", False, text,
    ):
        raise ValueError("incomplete or contradictory CLI result")
    return text


def sdk_command(model, system):
    # Private SDK surface, deliberately pinned and measured in the opt-in image.
    from claude_agent_sdk import ClaudeAgentOptions
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport

    options = ClaudeAgentOptions(
        cli_path=str(Path(__file__).with_name("fake-claude")), model=model,
        tools=[], mcp_servers='{"mcpServers":{}}', strict_mcp_config=True,
        setting_sources=[], system_prompt=system,
    )
    return SubprocessCLITransport(prompt="", options=options)._build_command()


def cli_completion(messages, system, model):
    envelope = {"type": "user", "message": {"role": "user", "content": json.dumps(messages)}}
    result = subprocess.run(sdk_command(model, system), input=json.dumps(envelope) + "\n",
                            text=True, capture_output=True, timeout=15, check=True)
    return cli_text(result.stdout)


def fake_cli():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    for flag in ("output-format", "input-format", "tools", "mcp-config", "system-prompt", "model"):
        parser.add_argument("--" + flag)
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--strict-mcp-config", action="store_true")
    parser.add_argument("--setting-sources", required=True)
    args = parser.parse_args()
    if (args.output_format != "stream-json" or args.input_format != "stream-json"
            or args.tools != "" or json.loads(args.mcp_config) != {"mcpServers": {}}
            or not args.strict_mcp_config or args.setting_sources != ""):
        raise ValueError("unsafe or unsupported fake CLI command")
    envelope = json.load(sys.stdin)
    messages = json.loads(envelope["message"]["content"])
    capture({"source": "fake-cli", "messages": messages, "system": args.system_prompt,
             "argv": sys.argv[1:]})
    text = provider_text(messages, args.system_prompt)
    for row in cli_records(text):
        print(json.dumps(row))


def provider_text(messages, system):
    if system.startswith("You produce concise summaries as JSON."):
        return '{"tldr":"synthetic summary","full_summary":null}'
    return response(os.environ["CONTROL_KIND"])


def adapter():
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--model", required=True)
    parser.add_argument("--max-tokens", required=True)
    parser.add_argument("--system-prompt-file", type=Path)
    parser.add_argument("--effort")
    parser.add_argument("--thinking", nargs="?", const="enabled")
    parser.add_argument("--no-stream", action="store_true")
    args = parser.parse_args()
    messages = json.load(sys.stdin)
    system = args.system_prompt_file.read_text() if args.system_prompt_file else ""
    capture({"source": "adapter", "messages": messages, "system": system,
             "thinking": args.thinking, "max_tokens": args.max_tokens, "effort": args.effort})
    text = (cli_completion(messages, system, args.model) if os.environ["TEXT_ROUTE"] == "cli"
            else provider_text(messages, system))
    print(text, end="")


if __name__ == "__main__":
    try:
        fake_cli() if Path(sys.argv[0]).name == "fake-claude" else adapter()
    except (ValueError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(7) from exc
