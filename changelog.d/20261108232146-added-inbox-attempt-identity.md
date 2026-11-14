- **Inbox traces can show reported CLI identities for observed evaluation attempts.**
  Available result identities survive classified errors and appear on separate
  invoker spans under the inbox batch. Tracing remains optional, private execution
  omits the new invoker attributes, and missing telemetry never implies that no
  work ran. Retry, approval and response handling keep their existing behavior.
