- **Tools are reported as requested, not as run.** The runtime records the
  moment the model asks for a tool, which is before anything happens — a call a
  safety hook then denies looks identical. The line shown to the reviewer says
  so instead of asserting the tool ran.
