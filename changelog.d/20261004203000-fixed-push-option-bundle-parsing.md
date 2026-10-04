- **The push guard now parses a bundled push-option value the same everywhere.**
  For `git push -uo ci.skip origin HEAD` the guard used to read `ci.skip` as the
  remote and ask again on an eligible re-push, and it read `-o -f` / `-o +x` as
  a force push. One shared argument scanner now feeds the remote, positional,
  and force checks, so an option value is always data and never a flag, remote,
  or refspec.
