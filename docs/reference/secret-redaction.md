# Secret-value redaction in Claude Code sessions

`plugins/genesis-secret-redact` keeps the values in an install's `secrets.env` out of everything a Claude
Code session reads or keeps. It works on the VALUES, so it does not matter how a command reached them: `cat`,
`env`, `/proc/<pid>/environ`, `grep -r`, a script, an MCP tool or a subagent. A value is
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
  that is neither `on` nor `off`), and 2 when it cannot tell (no `claude`, unreadable `genesis.yaml`). When the
  install matches, it reads what sessions report about themselves: each session's plugin writes
  `~/.genesis/state/secret-redact/<session>.json` (names and counts only) after each load attempt, and `check`
  exits 4 when a session in the last day could not load the values (it ran unredacted, or on values loaded
  earlier) or found placeholder text stored as a value in the secrets file; a session that keeps failing
  refreshes its report hourly. No report in the last day is said to be unknown (exit 0): `check` cannot tell
  "no session ran" from "a session could not write its report". `apply` removes reports older than a week. Surfacing `check` in Genesis's health alerts is #4441.
- Only the main checkout registers itself; a linked worktree is refused. A checkout that moved is re-pointed on
  the next run, keeping a disabled plugin disabled.
- No `claude` on PATH: the step reports it and is re-attempted on the next update.

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

`bin/publish_guard.py`, a second PreToolUse hook in the plugin, refuses a Bash or Monitor command that mentions
`gh` (run directly, through a wrapper such as `timeout`, `bash -c` or `find -exec`, through a variable assigned
in the same command, or handed as one string to a launcher such as `eval`, `watch`, `script -c`, `flock -c`,
`su -c`, `tmux new-session` or `parallel`) when a known value appears in the command text, in a word the shell
would pass to a program, or in an existing file the command names. The command is read with the repository's
shared shell parser (`scripts/hooks/shell_parse.py`), so nested commands (`bash -c`, `$(...)`, backticks, and
the launcher strings above, parsed as commands to the parser's depth bound), redirect targets (`<f`, `$(<f)`,
`<"$HOME/f"`), assignment values (`F=f; gh ... -F "$F"`), globs, `~`/`$HOME` and the directories `cd`, `pushd`
and `env -C` move into are followed. Every word, every part after an `=`, a short option's attached value
(`-Ff`) and each of those without a leading `@` is tried as a path, so `--body-file f`, `-F body=@f` and
`--field=body=@f` are covered without modelling gh's flags. A value in the command text is refused at once,
without reading files. Files are read up to 16 MiB, a glob is followed to its first 64 files, and the file
check stops after 8 seconds (the hook's own timeout is 15); in each case the agent gets a one-line note saying
what was not checked.

A gh-mentioning command the parser cannot prove it read completely (a line continuation, a here-doc it cannot
tokenize, a `$'...'` string that builds text, and the other blind spots the parser reports) cannot be checked.
While values are loaded it is refused, with the parser's reason and its way out, when any gh in it may publish:
`pr create|comment|edit|review|merge`, `issue create|comment|edit`, `api`, `release` or `gist`, found in the raw
text or with quotes, backslashes and continuations removed. Because the parser cannot be trusted here the match
is lenient: a gh whose subcommand the text does not clearly show as a read (a variable, an unknown subcommand,
the word gh in prose) counts as publishing. When every gh in it clearly only reads (`pr view|list|checks`,
`issue view|list`, `repo view`, `run view|list|watch|download`, `workflow view|list`, `search`, `auth`,
`secret`...) and no value is in its text, it
goes ahead with a one-line note to the agent that the value check did not run. Writing a body to a file and
passing it with `--body-file` keeps a command readable. A launcher's string that cannot be read counts as an
unreadable command; another program's string that mentions gh (a `git commit -m` message) is data and is skipped
when it cannot be read. Measured on a live install (2026-11-13, 14,019 distinct gh-mentioning Bash commands from
the 150 most recent session transcripts): 715 (5.1%) are refused as unreadable, of which 537 the push guard
already refuses on its own, so 178 (1.3%) are new refusals; 163 pass with the note.

`gh secret` and `gh auth` are exempt: sending a value to GitHub's secret store or logging in is their job. The
exemption applies only when every gh the command runs is proven to be one of them, so an unreadable command
with a value in its text, or one where gh runs through a variable or another program, is not exempt. The
refusal names the variable and where it was found, never the value; a value inside a path is shown as its
placeholder.

If the values cannot be loaded the command goes ahead and stderr says so. If the parser cannot be loaded only the
raw command text is checked. A value already found in the command text is refused even when a later step fails.
Pushed commits are left to the repository's pre-push scan.

Not covered:

- a file the same command writes before gh runs (`... > b.md && gh ... -F b.md`): a PreToolUse hook sees only
  files that already exist;
- variables other than `HOME` that the command itself does not assign (set by an earlier call, a loop
  variable), including a value that reaches gh from the environment (`--body "$VAR"`), tracked in #4573;
- brace expansion (`{a,b}.md`), `cd -`, `popd`, `CDPATH` and `sudo -D`;
- a string handed to a program that is not a launcher (`ssh host '...'`) when the parser cannot read it;
- `$PWD`, `$(pwd)`, `$TMPDIR` and other variables in a path (only `~` and `$HOME` are resolved);
- a gh name the command builds from pieces (`G=g; ${G}h ...`): no gh is mentioned, so nothing is checked;
- files over 16 MiB, files past a glob's first 64, and files left when the 8-second check stops.

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
- **Using a value.** A placeholder is display text, not the value: a command that needs a key reads it from the
  environment or the file (`set -a; . <secrets file>; set +a`, or a tool that loads it), never by pasting
  `[secrets.env:VAR]` into the command line.
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
Claude Code's debug log (`claude --debug`) and the session's report, which `scripts/cc_plugins.py check` reads
(see "Install, upgrade and off-switch").

- The values cannot be loaded: the session runs unredacted. A reload that fails later keeps the values that
  loaded earlier. After a failed load the next attempt waits 30 seconds, so a loader that keeps failing does
  not run on every event.
- Redacting one tool result fails: a text-level replacement is tried, then the result goes through unchanged.
  The message content of its stored row is still redacted; the structured record of that result is not.
- Redacting a stored row fails twice: that row's text is withheld with a notice naming the plugin.
- A saved large output is not cleaned (it is not in a session's `tool-results` folder, or it cannot be read
  or written): the model's copy is still redacted; the disk copy is logged as possibly raw.
- The publishing guard cannot load the values: the gh command goes ahead and stderr says the check did not run.
  It cannot load the shell parser: only the raw command text is checked. Any later step fails: a value already
  found in the command text is still refused; otherwise the command goes ahead.

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
