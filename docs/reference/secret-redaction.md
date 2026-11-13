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
| A large output Claude Code saves to a session's `tool-results/` folder | the saved file is rewritten in place (up to the 4 MiB the engine's file read accepts; a larger one is logged as possibly raw) |
| The typed prompt (transcript and `~/.claude/history.jsonl`) | `prompt.submit` |

Which values: those of at least 16 characters whose variable NAME marks them as secret (`KEY`, `TOKEN`,
`SECRET`, `PASS`, `PWD`, `CRED`, `AUTH`, `PRIVATE`, `COOKIE`, `SESSION`, `WEBHOOK`, `DSN`, `SALT`, `SIGNING`,
`HMAC`, `BEARER` anywhere in the name, or `PAT`, `PW`, `SK`, `PSK` as a whole word of it; any case), plus any
URL with credentials in it (`scheme://user:pass@host`, `scheme://:pass@host`, or a token of 16 or more
characters alone before the `@`) whatever it is called. `bin/known_values.py` is the one place this is
decided; the file it reads is the one `src/genesis/secrets_location.py` resolves for the session's
environment, parsed with python-dotenv (the runtime's own parser) from the install's venv. Where there is no
venv, the loader's own port of python-dotenv's grammar and `${VAR}` interpolation reads it; a differential
test holds the two to the same result.

### Spellings

A value is matched as text, in the spellings below. This list is the contract: a spelling that is not on it
is not redacted.

| Matched | Not matched |
|---|---|
| The value as the secrets file spells it (a quoted value keeps its escapes) and as parsed | A value split by a wrapping tool (a folded table, base64 wrapped at 64 or 76 columns); the literal text of a value built with `${OTHER}` (only the value it resolves to is matched) |
| Percent-encoded and form-encoded (every reserved character encoded) | Percent-encoding that leaves some reserved characters (JavaScript's `encodeURIComponent` leaves `!*'()`), lowercase hex, double encoding |
| JSON as Python, JavaScript, jq, Go and PHP encoders write it | JSON inside JSON; Python `repr`; YAML's own escapes; Gson's and .NET's escapes; JSON or base64 of the file's escaped spelling of a quoted value |
| HTML-escaped as Python's `html.escape` writes it (quotes included) | Other HTML escapers' forms of a value holding `" '` (numeric entities, quotes left alone); shell quoting (`export -p`, `printf %q`, `set -x`) of a value holding `$ " ' \` |
| Inside base64 or base64url, at any byte alignment | Hex |
| The password, or the token standing in for a user, of a credential URL | Other parts of a URL value on their own |
| Each line of a multi-line value that is 16 characters or longer (PEM armor lines excepted) | A line under 16 characters, on its own; a field of a JSON-valued secret once a tool has decoded it |

A key of letters, digits, `-` and `_` has one spelling in every row of the left column except base64, so the
right column concerns URL-shaped secrets, passwords with punctuation and multi-line values.

### Placeholders

Every matched text has a placeholder of its own. The text the secrets file holds for a variable is
`[secrets.env:VAR]`; any other spelling of that value is `[secrets.env:VAR~1]`, `~2` and so on; line 2 of a
multi-line value is `[secrets.env:VAR#2]`. A value the session held earlier and that has since left the file
keeps the name it was shown under, or `[secrets.env:VAR~earlier]` when the new value took that name. Two
variables holding the same text show one placeholder, named after the first of them. A placeholder is
redacted once: text that already holds one passes through the next hook unchanged.

## Publishing guard

`bin/publish_guard.py`, a second PreToolUse hook in the plugin, refuses a Bash or Monitor command that runs `gh` (directly or through a wrapper such as `timeout` or `bash -c`) when a
known value appears in the command text or in an existing file of up to 1 MB that the command names (any token,
or the part after `=` in a token, is tried as a path, so `--body-file f`, `-F body=@f` and every other spelling
are covered without modelling gh's flags). `gh secret` and `gh auth` are exempt: sending a value to GitHub's
secret store or logging in is their job. The refusal names the variable and where it was found, never the value.
If the check cannot run it lets the command through and says so on stderr. Pushed commits are left to the
repository's pre-push scan.

## Behaviour worth knowing

- **Writing to the secrets file.** Placeholder text is never written into the secrets file. In an Edit whose
  target lands on the secrets file itself (`~`, `..`, a relative path or a symlinked directory all resolve),
  the placeholders in `old_string` (the text being replaced) are turned back into the texts they stood for,
  so the session can name a line it read: rotating a key in place with Edit works. The text it writes
  (`new_string`, or a Write's `content`) must hold real values only. A placeholder's meaning can move (a key
  rotates; a resumed session never saw the text an old name stood for), and a wrong restore in `old_string`
  only fails the Edit's exact match, so the file never receives a value that was not written as such. A
  placeholder written anywhere else stays literal text. Claude Code refuses a write through a symlink to the
  file outright (measured); a hard link is a different path to the same file and is not recognised.
- **One refusal.** An Edit or Write of the secrets file whose written text holds placeholder text is refused,
  with a message naming the placeholders. So is any Edit or Write carrying placeholder text while no values
  have loaded (the file's path is not known yet), and one the plugin failed to check. This is the only call the plugin blocks. In practice: a Write of
  the whole file as read is refused, and adding a line next to a line holding a secret means anchoring the
  Edit on a line without one. Onboarding (filling in an empty value) needs no placeholder at all.
- **A shell write to the secrets file** (`sed -i`, a heredoc) does not go through Edit/Write, so a placeholder
  in it lands in the file as text and that key is gone. The plugin does not repair this (tracked in #4453). It
  reports it: a variable whose value in the file is placeholder text is never treated as a secret value, and
  the plugin logs which variables are affected so they can be restored from a backup.
- **A copy of a value in another file** is shown as its placeholder too. An Edit whose `old_string` spans
  that line does not match, and a Write of that whole file as read stores the placeholder there: outside the
  secrets file nothing is restored, by design.
- **Errored tool calls** (a non-zero exit, an MCP tool raising) whose error text holds a value are answered with
  the same text redacted, which the model reads as the call's error result, exactly as it would have.
- **Rotation.** A session keeps redacting every value it has loaded, so a key it replaces in the secrets file
  stays hidden in that session (the Edit that replaced it echoes the old line back in its result). A session
  started afterwards knows only the values the file holds then.
- **Short secrets** (under 16 characters) are not redacted: matching them would rewrite ordinary text. A long
  line of a multi-line value that is ordinary text (a line of a JSON credential such as
  `"type": "service_account",`) IS matched, wherever it appears.
- **A key added or rotated by a tool call** is picked up before that call's result is redacted, so a command
  that writes a new key and prints it shows the placeholder. The plugin watches every path the resolver
  consults (the `SECRETS_PATH` override when set, else the home config file and the in-checkout places), so a
  secrets file the resolver would now pick is loaded too. (A call that starts while another event is still
  reloading uses that reload's values; a key its own tool then adds is redacted from the next call on.)

## Failure directions

Apart from the one refusal above, every failure here lets the session keep working. Its record is a line in
Claude Code's debug log (`claude --debug`); a health check that surfaces it without a debug run comes with the
install step.

- The values cannot be loaded: the session runs unredacted. A reload that fails later keeps the values that
  loaded earlier. After a failed load the next attempt waits 30 seconds, so a loader that keeps failing does
  not run on every event.
- Redacting one tool result fails: a text-level replacement is tried, then the result goes through unchanged.
  The message content of its stored row is still redacted; the structured record of that result is not.
- Redacting a stored row fails twice: that row's text is withheld with a notice naming the plugin.
- A saved large output is not cleaned (it is not in a session's `tool-results` folder, or it cannot be read
  or written): the model's copy is still redacted; the disk copy is logged as possibly raw.

## Not covered

- Copies made before the plugin was installed.
- A value typed or pasted into a prompt before the secrets file holds it (onboarding by paste): it is not a
  known value yet, so the prompt row and the Edit that stores it keep it.
- Values the file does not bind: a commented-out assignment (`# OLD_API_KEY=...`), and the earlier value of a
  name that is bound twice. `cat` of the file shows both. A file attached to the prompt (an @-mention of the
  secrets file) is not measured. All three are tracked in #4454; attachments, context and the system prompt
  in #4458.
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
  python-dotenv and redact-then-restore round trips over quoted values and over files whose texts collide.
- `tests/test_plugins/test_secret_redact_mod.py`: the mod (CI), run under node against a fake of Claude Code's
  engine with the real loader behind it.
- `scripts/e2e_secret_redact.py`: a real Claude Code session with canary values, run with the plugin loaded
  through `--plugin-dir`. It checks, step by step, that each scripted call ran and came back holding the
  placeholder, then the model's answer, the transcript, subagent transcripts and persisted output, and the
  secrets file after a refused Write of the whole file and two Edits. Needs a logged-in `claude` and spends one short
  session. The typed prompt is not part of it (a `-p` run keeps the engine's own record of its prompt); that
  path was measured in an interactive session.

Requires Claude Code 2.1.287 or later (mods on by default). On 2.1.299, measured: the mod also runs in
sessions started with `CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=0`.
