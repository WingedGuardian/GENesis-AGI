- **Turning off contribution offers or span tracing from the settings page now takes
  effect.** Both settings were saved to the user config folder and reported as
  applied, but the commit hook and the span loader only looked in the repo's
  `config/` folder, so the change was never read. Both now check the user overlay
  first, the same way the other config loaders do.
