- **Push-argument parsing now follows Git, in one shared scanner.** The push
  guard read several push spellings differently from Git: a value after a
  bundle-ending `-o` (`-uo -f`, `-uo +x`), a `git -c +k=v` global value,
  `origin -- -f`, a `--repo=fork` that is only an `-o` value, Git's unique
  long-option abbreviations (`--mirr` is a real `--mirror`), and which
  destination wins when a push names both `--repo` and a positional repository
  (Git uses the positional one). A forced push to origin spelled any of these
  ways now gets the force block instead of an approval prompt.

  Two spellings now get the lighter path, because Git treats them as plain
  pushes: `git push -- origin HEAD` and abbreviated safe flags such as
  `--set-up` can ride the re-push path. `git push --force --repo origin backups`
  now asks instead of blocking, because Git pushes it to `backups`.
