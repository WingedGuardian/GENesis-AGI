# Shipped lessons

Each `*.yaml` file in this directory is one lesson: a piece of advice the
tool-time advisor shows when a session calls one of the lesson's tools with
input that matches one of its patterns. `scripts/seed_procedures.py` loads
the files into `procedural_memory` on bootstrap and update. A lesson shipped
here is marked `shipped` and `reviewed`, so it is live on every install.

## Fields

| Field | Required | Meaning |
|---|---|---|
| `task_type` | yes | Short topic slug, e.g. `deploy_detach`. |
| `principle` | yes | The advice itself, one or two sentences. |
| `steps` | yes | A list of concrete steps (may be empty). |
| `tool_trigger` | yes | The tools the lesson watches, by their exact Claude Code names (`Bash`, `Edit`, `mcp__<server>__<tool>`). A shell command such as `ssh` is written as `Bash`, with a pattern. |
| `patterns` | yes | Regular expressions matched against the tool's input (case-sensitive; start a pattern with `(?i)` to ignore case); any one matching is enough. At most 8, each at most 200 characters, each must compile and must not match empty input. |
| `repeat` | yes | `session` (shown once per context window and agent, so again after a compaction) or `every` (shown on every match). |
| `context_tags` | no | Topic tags used by recall. |

Unknown fields make the file invalid.

## Rules

- **One lesson per file.** The file name, without `.yaml`, is the lesson's
  identity: renaming a file retires the old lesson and creates a new one.
- **Removing a file retires its lesson** on the next seed. A file that fails
  to load is reported and skipped, and its earlier version stays in place.
- **Public content.** These files ship in the public repository, so they
  carry technical text only: no hostnames, addresses, paths that name a
  user, or anything about a particular install.

## Example

```yaml
task_type: deploy_detach
principle: Launch a long deploy detached, so the session's command timeout cannot kill it.
steps:
  - Start it with setsid and redirect its output to a log file.
  - Poll the log, rather than waiting on the process.
tool_trigger: [Bash]
patterns: ['deploy_code_only\.sh']
repeat: session
```
