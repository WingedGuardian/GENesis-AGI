Added explicit shared MCP startup with frozen operator limits, actual standalone
service initializers and owned provider cleanup. Registered resource work settles
before clients/caches close, including partial startup and signal shutdown. The
rollout remains off; unit readiness and client switching are separate changes.
