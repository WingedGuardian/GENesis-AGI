### Fixed

- Keep corrupt approval-resumed inbox batches retryable when the retry cap was lowered while approval was pending, so safe item re-derivation can request fresh approval.
