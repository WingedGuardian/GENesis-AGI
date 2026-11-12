# Secret-value redaction in Claude Code sessions

`plugins/genesis-secret-redact` keeps the values in an install's `secrets.env` out of everything a Claude
Code session reads or keeps. It works on the VALUES, so it does not matter how a command reached them: `cat`,
`env`, `/proc/<pid>/environ`, `grep -r`, a script, an MCP tool or a subagent. A value is
replaced by `[secrets.env:VAR]`, so a session can still see that a key is set and which one it is.

It is a privacy measure for an honest session: values reaching the model, the transcript, memory built from
transcripts, backups and anything published from them. It is not a sandbox against a session trying to get a
value, and it does not stop a command from using one (`curl -H "Authorization: Bearer $KEY"` still runs with
the real key; only what is shown and stored is redacted).

## What is redacted

| Surface | How |
|---|---|
| Tool results the model reads, and the transcript's record of them (Bash, Read, Edit, MCP tools, subagents) | the plugin's mod rewrites the result in `tool.call` |
| The message content of every row the session stores (incl. errored tool calls and hook context) | `session.append` |
| A large output Claude Code saves to `tool-results/` | the saved file is rewritten in place |
| The typed prompt (transcript and `~/.claude/history.jsonl`) | `prompt.submit` |

Which values: those of at least 16 characters whose variable NAME marks them as secret (`KEY`, `TOKEN`,
`SECRET`, `PASS`, `PWD`, `CRED`, `AUTH`, `PRIVATE`, `COOKIE`, `SESSION`, `WEBHOOK`, `DSN`, `SALT`, `SIGNING`,
`HMAC`, `BEARER` anywhere in the name, or `PAT`, `PW`, `SK`, `PSK` as a whole word of it; any case), plus any
URL with credentials in it (`scheme://user:pass@host`, `scheme://:pass@host`, or a token of 16 or more
characters alone before the `@`) whatever it is called. Each is matched in every spelling
output can carry it in: as parsed, as the secrets file spells it (a quoted value keeps its escapes there),
URL-encoded, JSON-escaped, and inside base64 or base64url at every byte alignment. Each line of a multi-line
value that is 16 characters or longer is matched too and gets a placeholder of its own
(`[secrets.env:VAR#2]` for the second line); PEM armor lines are left alone. `bin/known_values.py` is the one place this is decided; the
file it reads is the one `src/genesis/secrets_location.py` resolves for the session's environment, parsed with
python-dotenv (the runtime's own parser) from the install's venv. Where there is no venv, the loader's own port
of python-dotenv's grammar and `${VAR}` interpolation reads it; a differential test holds the two to the same
result.

## Behaviour worth knowing

- **Writing to the secrets file.** A placeholder the session copied from a redacted read is turned back into
  the real value only when the write's target resolves to the secrets file itself (`~`, `..`, a relative path
  or a symlinked directory all resolve), so editing a key works. A placeholder written anywhere else stays
  literal text. Claude Code refuses a write through a symlink to the file outright (measured); a hard link is a
  different path to the same file and is not recognised, so a write through one stores the placeholder. The
  restored text is the spelling the file itself holds (a quoted value keeps its escapes), so a line the session
  did not change comes back byte for byte. A value the file builds with `${OTHER}` is restored as that
  expression. The restore text is right for the line it came from: a placeholder moved into a line with other
  quoting may need its escapes adjusted.
- **A shell write to the secrets file** (`sed -i`, a heredoc) does not go through Edit/Write, so a placeholder
  in it lands in the file as text and that key is gone. The plugin does not repair this (tracked in #4453). It
  reports it: a variable whose value in the file is placeholder text is never treated as a secret value, and
  the plugin logs which variables are affected so they can be restored from a backup.
- **Errored tool calls** (a non-zero exit, an MCP tool raising) whose error text holds a value are answered with
  the same text redacted, which the model reads as the call's error result, exactly as it would have.
- **Rotation.** A session keeps redacting every value it has loaded, so a key it replaces in the secrets file
  stays hidden in that session (the Edit that replaced it echoes the old line back in its result). A session
  started afterwards knows only the values the file holds then.
- **Short secrets** (under 16 characters) are not redacted: matching them would rewrite ordinary text. The
  same holds for a short line of a multi-line value. A long line of one that is ordinary text (a line of a
  JSON credential such as `"type": "service_account",`) IS matched, wherever it appears.
- **A key added or rotated by a tool call** is picked up before that call's result is redacted, so a command
  that writes a new key and prints it shows the placeholder.

## Failure directions

Every failure here lets the session keep working. Its record is a line in Claude Code's debug log
(`claude --debug`); a health check that surfaces it without a debug run comes with the install step.

- The values cannot be loaded: the session runs unredacted. A reload that fails later keeps the values that
  loaded earlier. After a failed load the next attempt waits 30 seconds, so a loader that keeps failing does
  not run on every event.
- Redacting one tool result fails: a text-level replacement is tried, then the result goes through unchanged;
  its stored row is still redacted.
- Redacting a stored row fails twice: that row's text is withheld with a notice naming the plugin.
- A saved large output cannot be cleaned: the model's copy is still redacted; the disk copy is logged as raw.

## Not covered

- Copies made before the plugin was installed.
- Values the file does not bind: a commented-out assignment (`# OLD_API_KEY=...`), and the earlier value of a
  name that is bound twice. `cat` of the file shows both. A file attached to the prompt (an @-mention of the
  secrets file) is not measured. All three are tracked in #4454.
- Values the selection rule does not reach: a secret under 16 characters, a secret whose name carries none of
  the markers above, and a credential URL whose password contains a `/`.
- Tool-call inputs and thinking blocks in stored rows: the engine keeps those as the model made them. They hold
  a value only if the model already had it, which is what the rest of this plugin prevents.
- Monitor events and background Bash output. Claude Code keeps its own record of those (queued notifications,
  task output files), which the mod cannot rewrite; what the model reads of them through a later tool call is
  still redacted. Redacting them at the source is tracked in #4445.
- What a settings-level PostToolUse hook stores: those hooks receive the tool's output before the mod rewrites it
  (measured). Genesis's `session_observer_hook` stores pattern-scrubbed summaries, which do not yet match known
  values; a third-party plugin's store (for example a tool-output archive) can hold a raw copy on local disk.
- Memory recall text a UserPromptSubmit hook injects: the model's copy is redacted, but Claude Code keeps the
  hook's own output as a record of its own (measured), so the recall hook has to filter at its source.
- The engine's record of a `claude -p` prompt (`queue-operation`).
- Sessions started with `--bare` or `--safe-mode`, which load no plugins.
- Other agent harnesses (Codex).

## Testing

- `tests/test_plugins/test_secret_redact.py`: the loader (CI), including the differential against
  python-dotenv and a redact-then-restore round trip over quoted values.
- `tests/test_plugins/test_secret_redact_mod.py`: the mod (CI), run under node against a fake of Claude Code's
  engine with the real loader behind it.
- `scripts/e2e_secret_redact.py`: a real Claude Code session with canary values, run with the plugin loaded
  through `--plugin-dir`. It checks, step by step, that each scripted call ran and came back holding the
  placeholder, then the model's answer, the transcript, subagent transcripts and persisted output, and the
  secrets file after a Write of the whole file and an Edit. Needs a logged-in `claude` and spends one short
  session. The typed prompt is not part of it (a `-p` run keeps the engine's own record of its prompt); that
  path was measured in an interactive session.

Requires Claude Code 2.1.287 or later (mods on by default). On 2.1.299, measured: the mod also runs in
sessions started with `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=0`.
