- **Guardian: no snapshot delete on a pool that has already recovered.** Pool
  relief now re-measures the pool immediately before deleting and stops if the
  shortfall has eased (an autoextend landing, space freed elsewhere). Before,
  it checked only that it was still the same pool. Delete-first rotation now
  tries the create once more after its measurements, so if the pressure has
  cleared it makes an ordinary create-then-delete rotation instead of deleting
  the rollback snapshot first.
