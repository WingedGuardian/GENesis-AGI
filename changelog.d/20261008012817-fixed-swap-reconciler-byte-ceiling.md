- Fixed the container-swap reconciler clobbering an operator- or Incus-native
  `limits.memory.swap` byte-ceiling value back to `true` on every tick, which
  would have flipped swap off continuously once such a value was set.
