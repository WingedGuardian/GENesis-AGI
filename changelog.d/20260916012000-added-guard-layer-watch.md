- **The Guardian can now see the agent tooling die.** A new host-side side-watch
  (`guardian/guard_layer_watch.py`, wired into `run_check`) probes whether the CC
  hook layer can still EVALUATE: the `genesis-hook` launcher runs end to end, the
  container venv interpreter runs, `hook_input` and `shell_parse` import, container
  `node` runs, and the host's own configured Claude Code binary starts. That last
  one probes the CONSUMER `diagnosis.py` actually launches rather than
  `node --version`, so a PATH failure is a true positive rather than a false one —
  host Node is frequently nvm-managed and a systemd timer's PATH is minimal.

  Two failure polarities motivate it, and the quiet one is the reason it exists.
  An unimportable `hook_input` fails CLOSED: every non-advisory Bash guard refuses,
  loud and unmissable. A dead launcher or venv makes `genesis-hook` exit non-2,
  which Claude Code treats as a non-blocking error, so every security guard is
  silently off while Bash keeps working and nothing on the install would report it.

  It must be host-side: a broken guard layer bricks CC sessions, and the
  container-side Sentinel is itself a CC call site, so it would dispatch into the
  same broken tooling — reasoning the repo already encodes independently in
  `sentinel/remediation_map.py`'s `UNMAPPED_BY_DESIGN`. It is deliberately NOT a
  `probe_*` in `collect_all_signals`: `SignalResult` carries no severity, so every
  probe there feeds the confirmation ladder into `RecoveryEngine.execute`, and a
  broken hook file must never be able to trigger a container restart. Alerts reach
  Telegram over stdlib urllib straight from the host, so they survive a fully dead
  container.

  **Alert-only, by decision rather than omission.** An earlier draft carried one
  automatic repair verb. An adversarial audit reproduced two ways it destroyed
  work — `git checkout HEAD -- <file>` overwrites the index, so staged content is
  lost and recoverable only via `git fsck`, and mid-merge the same command clears
  the conflict stages and silently resolves to ours while `MERGE_HEAD` remains, so
  the next commit drops the other side with no marker — and the probe's own
  dirty/clean signal failed OPEN, because `git diff --quiet` is tri-state and both
  error codes read as "differs", the value that authorised the write. The verb was
  removed rather than patched, and its config knobs are absent rather than
  defaulted off, since a disabled destructive path is still one config edit from
  running. A test asserts the module defines no repair function and that no
  executed payload carries a mutating git verb.

  Two further audit findings are fixed in the detection half: an episode that
  never reached a warning is now cleared when the condition goes healthy, rather
  than stranding state whose stale start timestamp silently expired every
  elapsed-time window on the next occurrence; and a condition whose probe returns
  INCONCLUSIVE is skipped rather than falling through the healthy branch, which
  had emitted a false "recovered" and reset a ladder that was still climbing. The
  host probe now uses the house `kill_process_group`/`reap_bounded` pair with
  `start_new_session`, matching `diagnosis.py`, so a wedged probe cannot orphan
  node children on a watch that runs every tick.
