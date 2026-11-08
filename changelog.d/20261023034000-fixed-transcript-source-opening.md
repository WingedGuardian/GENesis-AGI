- Transcript collection rejects a source replaced by a nonregular file before
  extraction. FIFO replacement cannot hold the writer waiting for a FIFO writer;
  failed refreshes retain the previously accepted source revision.
- Sidecar fingerprinting rejects nonregular or oversized metadata. Collection
  preserves its captured root, refuses corrupt bindings, and continues past
  individual source stat failures while recording incomplete coverage.
