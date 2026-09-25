- **An install can now switch off a named approval prompt, without loosening a
  single block.** Two guards ask before they act: the push guard, before a
  branch is published, and the credentials guard, before anything touches
  `secrets.env`. Both prompts are deliberate, and on an install where every push
  goes to the same public repo and the operator approves each one by reflex,
  both had stopped being decisions — a prompt answered without reading is a
  prompt that makes the ones worth reading harder to notice. `hooks.asks` in
  `~/.genesis/config/genesis.yaml` now names individual prompts and turns them
  off (`push_publish: off`, `secrets_env: off`); the public default is unchanged,
  so a clone with no local config behaves exactly as before. The allow that
  replaces a suppressed prompt names the setting that suppressed it, so the
  transcript still records that the credentials were touched or the branch
  published — "stop asking me", never "stop telling me". Only the routine publish
  approvals are suppressible: the force-push prompt is destructive, and the
  no-open-PR and close-then-push prompts report a state the publish rule forbids,
  so all three stay. Every hard block is out of reach by construction, as is the
  deny a dispatched session gets, and the vocabulary is a closed set — a config
  naming a prompt nobody classified does nothing at all. Every way of failing to
  read the setting (absent file, bad YAML, duplicate key, a value that is not a
  boolean) lands on the prompt rather than the allow.
