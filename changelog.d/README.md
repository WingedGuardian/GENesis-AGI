# changelog.d — one fragment per change

**Do not edit `CHANGELOG.md` in a pull request.** Add a file here instead. At
release time `scripts/assemble_changelog.py` folds every fragment into the
`[Unreleased]` section and deletes them.

## Why

Two branches that each add a changelog entry are not disagreeing about
anything — they are inserting at the same position, which git reports as a
conflict someone has to resolve by hand. Measured against this repository's own
queue on 2026-09-04: of 49 open pull requests, 21 could not merge, and **18 of
those 21 conflicted on `CHANGELOG.md` and nothing else** — every other file in
them merged cleanly.

Two branches writing two different filenames have nothing to merge. That is the
whole idea, and it is why the fix is a directory rather than a cleverer merge
driver: GitHub ignores a repository's `.gitattributes` server-side, so no merge
setting we commit can change what GitHub reports for a shared file. Measured —
GitHub's own merge engine returns a conflict for a `CHANGELOG.md` collision both
with and without a `merge=union` attribute present.

## Naming

    changelog.d/<YYYYMMDDHHMMSS>-<category>-<slug>.md

- **Timestamp** — UTC, from `date -u +%Y%m%d%H%M%S`. It is the sort key within a
  category, and it is what makes two branches picking the same name unlikely —
  not impossible: one-second resolution means two branches writing the same
  category and slug in the same second collide. That collision is an ordinary
  git add/add conflict, which git itself reports and a human resolves by
  renaming one file; nothing is lost silently. Same convention as migration
  ids, for the same reason.
- **Category** — one of `added`, `changed`, `deprecated`, `removed`, `fixed`,
  `security` (Keep a Changelog).
- **Slug** — lowercase, dash-separated. For humans scanning the directory.

Example: `20260904210000-fixed-changelog-collisions.md`

## Content

The file holds the entry exactly as it should appear under its category
heading — a Markdown bullet, spliced in verbatim:

```markdown
- **The thing a user would notice, stated as an outcome.** What changed and
  why it matters, in prose. Continuation lines are indented two spaces and
  blank-line-separated paragraphs are fine.
```

It must start with `- `. The audience is a user updating their install, not a
reviewer reading the diff — say what changed for them, not which function moved.

## Commands

```bash
scripts/assemble_changelog.py --check     # validate every fragment
scripts/assemble_changelog.py --dry-run   # print the resulting CHANGELOG.md
scripts/assemble_changelog.py             # fold in and delete the fragments
```

Assemble at **release** time, not per pull request. Folding early re-creates the
single shared file everyone edits, which is the collision this directory exists
to prevent.

## What is checked

Every entry in this directory is classified, and anything that is neither a
fragment nor this README is an error rather than a skipped file — a silently
ignored fragment is a changelog entry that never ships. That covers a malformed
name, a timestamp that is fourteen digits but not a real date, an unknown
category, an empty or unreadable file, content that does not start with a
bullet, and a nested subdirectory.

Because the body is spliced in **verbatim**, it also rejects anything that would
read as a new top-level block once it lands inside `[Unreleased]`: a Markdown
heading of any level, a setext underline or thematic break, a code fence in
either fence character, and an HTML block. Indent it **four** spaces and it
stays inside the bullet, which is where it belongs — two is not enough under a
wider bullet, and CommonMark treats up to three spaces as unindented.

What that protects is how the file *reads*, not the assembler: the fold anchors
on the `[Unreleased]` heading and never computes a section end, so a stray `## `
cannot orphan anything. It can, though, produce a phantom release that a person
— or anything splitting the file on `## `, which is what publishing a release
does — mistakes for a real one, and an unclosed fence renders every entry below
it as code.

Editor debris (`.DS_Store`, `.gitkeep`, `.foo.md.swp`) is skipped so local mess
cannot block a release — but that exemption stops at Markdown. A committed
`.20260904…-fixed-x.md` is classified like any other `.md` and errors, because
skipping it would hide an entry in exactly the way this directory exists to
prevent.

CI validates every fragment on every pull request, and also refuses a change
that deletes a fragment the base branch already had — before assembly a fragment
is the only copy of its entry. The one legitimate deletion is the release fold,
which removes the fragments and rewrites `CHANGELOG.md` together.
