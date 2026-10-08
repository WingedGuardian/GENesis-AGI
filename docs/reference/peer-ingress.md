# Dashboard and peer ingress

Flask binds to container `127.0.0.1:5000`. New host installations forward
host `127.0.0.1:5000` to that container loopback socket. A public Incus proxy
would make remote callers appear local to Flask; a bearer token does not
repair that loss of source identity on owner RPC routes.

For immediate owner access, use an SSH tunnel:

```sh
ssh -L 5000:localhost:5000 <host-user>@<host-address>
```

Open `http://localhost:5000/genesis` on your device. Configure a dashboard
password before enabling HTTPS access. Dashboard authentication does not
protect every owner API read, so peer devices must never reach the owner
root proxy.

## Target proxy topology

| Host ingress | Backend and scope | Permitted callers |
|---|---|---|
| loopback TCP 5000 | existing container-loopback Flask | host processes / owner SSH tunnel |
| tailnet HTTPS 9443 | owner root on loopback 5000 | explicitly selected owner devices only |
| tailnet HTTPS 443 | scoped peer path; independently enabled bearer-gated desk, voice, completions paths | specifically authorized clients |
| private HTTPS 8443 | SAM control plane/router | enrolled nodes and owner administration only |

The peer and SAM mounts require their later components and acceptance checks;
this ingress prerequisite does not activate them. No broad `/api` or `/v1`
mount belongs on port 443. Do not enable Funnel or a public TCP listener.
SAM forwarding requires its backend credential and verified identity; direct
fallback credentials must not make client-supplied identity headers trusted.

[Tailscale Serve](https://tailscale.com/docs/features/tailscale-serve) supplies
tailnet HTTPS. Configure the entire effective tailnet policy first. Access
rules are additive: a narrower new rule cannot undo an existing broad allow.
Use explicit device selectors for owner access, rather than an identity that
also owns peer VMs. Existing grants and ACLs both need examination. Add policy
tests for owner acceptance on 9443 and peer denial on 9443/5000; the policy
test system checks both grants and ACLs. See the
[policy reference](https://tailscale.com/docs/reference/syntax/policy-file#tests).
Then test real negative requests from the actual peer device, including owner
RPC routes. A successful allowed request alone proves no isolation.

Once the owner restriction passes, an operator may configure the owner mount:

```sh
sudo tailscale serve --bg --https=9443 http://127.0.0.1:5000
tailscale serve status --json
```

Keep the returned configuration and policy-test results privately. Verify the
owner device can open the dashboard and the peer device cannot connect to
9443. Audit existing root mounts on other ports before peer activation; do not
blindly reset Serve, which can remove unrelated operator services.

## Existing installation migration

Deploy the reviewed Guardian code through the existing update scripts first.
Run the following on the host with its deployed Guardian interpreter, retaining
an SSH session/tunnel. Use the actual installation path if it differs.

```sh
~/.local/share/genesis-guardian/.venv/bin/python -m genesis.guardian.dashboard_ingress \
  --config ~/.local/share/genesis-guardian/config/guardian.yaml
~/.local/share/genesis-guardian/.venv/bin/python -m genesis.guardian.dashboard_ingress \
  --config ~/.local/share/genesis-guardian/config/guardian.yaml --apply
```

The default is a read-only preflight. Apply requires a standard dashboard proxy
connecting to container loopback, port 5000, healthy host-loopback HTTP and
configured dashboard authentication. It atomically updates only
`health_api_host`, preserving comments, other YAML values and file mode, before
restricting the existing Incus listener. It confirms the new listener and HTTP
readiness. Unknown topology, ambiguous YAML, a conflicting shell override or an
Incus failure returns failure. An uncertain network change is never rolled back
to a public listener. Inspect the real error and current device configuration;
do not activate peers after a partial migration.

Guardian reads `health_api_host` for HTTP only; `GUARDIAN_HEALTH_HOST` takes
precedence, and clearing it restores the configured value to an empty override
(which uses the container address). `GUARDIAN_HEALTH_PORT` also overrides the
port. Inspect the deployed unit's configured overrides privately before applying
the migration. ICMP independently uses the configured or autodetected container
address and refuses to report the host loopback as an autodetection success.
The next Guardian timer invocation reloads YAML; no server binding change is
needed. The installer aligns an unset HTTP target only when it observes the
loopback Incus listener, retaining explicit operator targets.

After apply, verify host local HTTP and Guardian's check-only probes, owner
HTTPS/tunnel access, and failed LAN/tailnet TCP-5000 access. Keep peer admission
closed until the effective policy, scoped mounts, credentials and full peer
approval/result acceptance pass. This build does not generate production
credentials, enroll nodes, restart services or change the live tailnet policy.

Rollback keeps admission closed and the loopback listener restricted. Repair
the HTTP override or use the owner tunnel; reopening port 5000 to the LAN is
not a safe rollback for an active peer installation.
