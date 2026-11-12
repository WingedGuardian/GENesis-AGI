# Secret-value redaction in Claude Code sessions

`plugins/genesis-secret-redact` keeps the values in an install's `secrets.env` out of everything a Claude
Code session reads or keeps. It works on the VALUES, so it does not matter how a command reached them: `cat`,
`env`, `/proc/<pid>/environ`, `grep -r`, a script, an MCP tool, a subagent or a background task. A value is
replaced by `[secrets.env:VAR]`, so a session can still see that a key is set and which one it is.

It is a privacy measure for an honest session: values reaching the model, the transcript, memory built from
transcripts, backups and anything published from them. It is not a sandbox against a session trying to get a
value, and it does not stop a command from using one (`curl -H "Authorization: Bearer $KEY"` still runs with
the real key; only what is shown and stored is redacted).

## Install, upgrade and off-switch

`scripts/bootstrap.sh` and `scripts/update.sh` run `scripts/cc_plugins.py apply`, which registers the checkout's
`plugins/` folder as the Claude Code marketplace `genesis` at user scope and installs `genesis-secret-redact`.
Every session on the install then loads it: foreground, subagents and dispatched `claude -p` sessions, except
sessions launched with `--bare` or `--safe-mode` (readiness probes, eval benches), which load no plugins. The plugin
loads in place from the checkout, so a later deploy updates it at the next session start with no reinstall.

- **Off:** set `cc_plugins.secret_redact: off` in `~/.genesis/config/genesis.yaml`; the next bootstrap or update
  uninstalls it. `claude plugin disable genesis-secret-redact@genesis` also works and is kept: `apply` never
  re-enables a plugin that is installed.
- **Check:** `scripts/cc_plugins.py check` prints the state and exits 3 when it does not match the lever (for
  example, disabled while the lever is on, the marketplace pointing at a checkout that moved, or a lever value
  that is neither `on` nor `off`), and 2 when it cannot tell (no `claude`, unreadable `genesis.yaml`). Nothing
  reports this to Genesis's health surfaces yet; that wiring is tracked separately.
- Only the main checkout registers itself; a linked worktree is refused. A checkout that moved is re-pointed on
  the next run, keeping a disabled plugin disabled.
- No `claude` on PATH: the step reports it and is re-attempted on the next update.

## What is redacted

| Surface | How |
|---|---|
| Tool results the model reads, and the transcript's record of them (Bash, Read, Edit, MCP tools, subagents) | the plugin's mod rewrites the result in `tool.call` |
| Every row the session stores (incl. errored tool calls, attachments, hook context) | `session.append` |
| A large output Claude Code saves to `tool-results/` | the saved file is rewritten in place |
| The typed prompt (transcript and `~/.claude/history.jsonl`) | `prompt.submit` |
| Monitor events, background Bash output, their task output files and queued notifications | `bin/stream_wrap.py`, a PreToolUse hook, runs those commands through `bin/redact_run.sh` |

Which values: those of at least 16 characters whose variable NAME marks them as secret (`KEY`, `TOKEN`,
`SECRET`, `PASS`, `PWD`, `CREDENTIAL`, `AUTH`, `PRIVATE`, `COOKIE`, `SESSION`, `WEBHOOK`, `DSN`), plus any URL
with a password in it (`scheme://user:pass@host`) whatever it is called. Each is matched in every spelling
output can carry it in: as written, URL-encoded, JSON-escaped, inside base64 or base64url at every byte
alignment, and line by line for a multi-line value. `bin/known_values.py` is the one place this is decided; the
file it reads is the one `src/genesis/secrets_location.py` resolves for the session's environment, parsed with
python-dotenv (the runtime's own parser) from the install's venv, or a fallback parser without `${VAR}`
expansion where there is no venv.

## Behaviour worth knowing

- **Writing to the secrets file.** A placeholder the session copied from a redacted read is turned back into
  the real value only when the write's target resolves to the secrets file itself (any spelling of its path), so
  editing a key works. A placeholder written anywhere else stays literal text. Claude Code refuses a write
  through a symlink outright (measured), so an alias cannot be used to write placeholders over the values. The
  restored text is the value as parsed: a line that used `${OTHER}` or a quoted escape is rewritten with its
  expanded value (same meaning, different text). A write that does not go through Edit/Write (a `sed -i` or a
  heredoc in Bash) gets no restore, so a placeholder there would replace the value with literal text.
- **Errored tool calls** (a non-zero exit, an MCP tool raising) whose error text holds a value are answered with
  the same text redacted, which the model reads as the call's error result, exactly as it would have.
- **Using a value.** A placeholder is display text, not the value: a command that needs a key reads it from the
  environment or the file (`set -a; . <secrets file>; set +a`, or a tool that loads it), never by pasting
  `[secrets.env:VAR]` into the command line.
- **Rotation.** Only current values are redacted. After a key is replaced in the secrets file, the old value is
  no longer this install's secret and is shown as it is.
- **Short secrets** (under 16 characters) are not redacted: matching them would rewrite ordinary text.
- **Monitor and background Bash** run inside Claude Code's own command shell (its shell functions, such as the
  `pkill` that refuses to kill the Claude process, are kept), with stdout and stderr each filtered. The command's
  exit status is its own; a background child it leaves running keeps its output and is not killed.
- **Other PreToolUse hooks see the original command.** The stream wrap rewrites only the command that runs.

## Failure directions

Every failure here lets the session keep working. Its record is a line in Claude Code's debug log
(`claude --debug`); a health check that surfaces it without a debug run comes with the install step.

- The values cannot be loaded: the session runs unredacted. A reload that fails later keeps the values that
  loaded earlier.
- Redacting one tool result fails: a text-level replacement is tried, then the result goes through unchanged;
  its stored row is still redacted.
- Redacting a stored row fails twice: that row's text is withheld with a notice naming the plugin.
- The stream wrap fails or times out (15 s; the hook reads one small file), or the runner cannot make its
  temporary FIFOs: the command runs unwrapped, and the runner says so on stderr.
- A saved large output cannot be cleaned: the model's copy is still redacted; the disk copy is logged as raw.

## Not covered

- Copies made before the plugin was installed.
- What a settings-level PostToolUse hook stores: those hooks receive the tool's output before the mod rewrites it
  (measured). Genesis's `session_observer_hook` stores pattern-scrubbed summaries, which do not yet match known
  values; a third-party plugin's store (for example a tool-output archive) can hold a raw copy on local disk.
- Memory recall text a UserPromptSubmit hook injects: the model's copy is redacted, but Claude Code keeps the
  hook's own output as a record of its own (measured), so the recall hook has to filter at its source.
- The engine's record of a `claude -p` prompt (`queue-operation`).
- A foreground Bash call that Claude Code moves to the background part-way through: it was not started through
  the stream runner, so its task output file is not filtered (what the model reads of it is still redacted).
- Sessions started with `--bare` or `--safe-mode`, which load no plugins.
- Other agent harnesses (Codex); see the plan for their own wrap.

## Testing

- `tests/test_plugins/test_secret_redact.py`: the loader, the stream runner and the wrap (CI).
- `scripts/e2e_secret_redact.py`: a real Claude Code session with canary values, run with the plugin loaded
  through `--plugin-dir`; checks the model's answer, the transcript, subagent transcripts, persisted output,
  task output files and the prompt history, and that sibling hooks saw original commands. Needs a logged-in
  `claude` and spends one short session.

Requires Claude Code 2.1.287 or later (mods on by default). On 2.1.299, measured: the mod also runs in
sessions started with `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=0`.
