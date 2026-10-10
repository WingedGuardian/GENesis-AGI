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

Keep peer admission disabled and retain an owner SSH session/tunnel. On existing
Guardian installations, use the deployed restricted gateway to establish an
operator-owned `pause <seconds>` and confirm `paused` reports an active pause
before the update. Choose a TTL within the deployed limit (at most 3600 seconds),
and renew it before expiry throughout the update, host synchronization and
migration. The update preserves an already-active operator pause and does not
resume it. If pause/paused support or confirmation is unavailable, stop and
resolve that prerequisite; do not assume the deployment's own pause covers host
synchronization. A fresh installation without Guardian has no pause to preserve.

Deploy through the full approved `scripts/update.sh` path: its bootstrap renders
the loopback server unit. Code-only deployment does not render changed units.
Verify the update and Guardian host synchronization actually succeeded; old
Guardian code must support the HTTP host override before migrating. The container
must provide `ss` (from `iproute2`) for the read-only live-listener preflight. Its
absence is a migration failure; this command does not install packages.

Run the following on the host with its deployed Guardian interpreter and source
path. Use the actual installation path if it differs.

```sh
PYTHONPATH="$HOME/.local/share/genesis-guardian/src" \
  ~/.local/share/genesis-guardian/.venv/bin/python -m genesis.guardian.dashboard_ingress \
  --config ~/.local/share/genesis-guardian/config/guardian.yaml
PYTHONPATH="$HOME/.local/share/genesis-guardian/src" \
  ~/.local/share/genesis-guardian/.venv/bin/python -m genesis.guardian.dashboard_ingress \
  --config ~/.local/share/genesis-guardian/config/guardian.yaml --apply
```

The default is a read-only preflight. Apply requires a standard dashboard proxy
connecting to container loopback, with `bind` unset or `host`, and `nat` and
`proxy_protocol` unset or `false`. Failed device reads and customized direction
or transport require operator inspection. It also requires an exact integer port 5000, an observed
loopback-only container listener, healthy host-loopback HTTP and configured
dashboard authentication. Dry-run also validates the prospective YAML patch and
deployed Guardian override support. Apply atomically updates only
`health_api_host`, preserving comments, other YAML values and file mode, before
restricting the existing Incus listener. It confirms the new listener and HTTP
readiness. Unknown topology, ambiguous YAML, a conflicting shell override or an
Incus failure returns failure. Preflight also requires a loaded, nontransient
standard Guardian user service with no pending daemon reload. It reads typed
systemd properties and merges the user-manager environment, service assignments
and final environment removals. The effective `GUARDIAN_CONFIG` must select the
supplied YAML; an alternate file is supported when it is the selected file.
Service-only host, port or container conflicts refuse before either mutation.
Environment files, PAM sources, wrappers/hooks, alternate Python paths and
filesystem or network namespace remapping require operator inspection. Missing
properties or an unavailable user bus are failures, not proof of compatibility.
The check trusts the deployed interpreter and installed Python environment;
it does not verify arbitrary Python startup hooks. Avoid concurrent service or
environment changes during this maintenance-window snapshot.
An uncertain network change is never rolled back
to a public listener. Inspect the real error and current device configuration;
do not activate peers after a partial migration.

Guardian reads `health_api_host` for HTTP only; `GUARDIAN_HEALTH_HOST` takes
precedence, and clearing it restores the configured value to an empty override
(which uses the container address). `GUARDIAN_HEALTH_PORT` also overrides the
port. Inspect the deployed unit's configured overrides privately before applying
the migration. ICMP independently uses the configured or autodetected container
address and refuses to report the host loopback as an autodetection success.
Guardian's health and dialogue requests bypass environment HTTP proxies only
for numeric loopback targets. Other configured targets retain normal urllib
proxy handling, including `no_proxy`; hostname aliases do not select the bypass.
The next Guardian timer invocation
reloads YAML. The installer aligns an unset HTTP target only when both Incus
endpoints are loopback on port 5000, those proxy modes are standard, and the effective health port is standard;
explicit operator targets and conflicting overrides are retained. Optional
alignment runs after the existing unit installation and successful daemon
reload, proves the loaded service profile, and preserves YAML with a safe skip
message if the profile is unproven. It does not start a service to obtain proof.

Host setup stops before installing Guardian if the dashboard proxy cannot be
created or verified. New devices are read back before setup proceeds; existing
unknown devices are retained for inspection rather than overwritten. Generated
network instructions identify the local loopback URL and owner SSH-tunnel or
explicitly configured authenticated HTTPS access, rather than host-LAN port 5000.
Before adding a device, setup requires successful structured Incus inspection
showing its absence in both local and profile-expanded devices. Ambiguous or
unreadable inspection stops setup. This uses the container Python installed
earlier in setup; it adds no host parser dependency. Avoid concurrent operator
topology changes during setup or migration: these preflights are not an Incus
configuration lock.

After apply, verify host local HTTP, Guardian's check-only probes and actual
dialogue, owner HTTPS/tunnel access, and failed LAN/tailnet TCP-5000 access to
both host and container addresses. Confirm the operator pause is still active;
resume explicitly through the gateway only after those checks pass. If update,
sync, migration or readiness fails, keep peers disabled, inspect the actual
state and continue maintaining the pause while repairing it. Keep peer admission
closed until the effective policy, scoped mounts, credentials and full peer
approval/result acceptance pass. This build does not generate production
credentials, enroll nodes, restart services or change the live tailnet policy.

Rollback keeps admission closed and the loopback listener restricted. Repair
the HTTP override or use the owner tunnel; reopening port 5000 to the LAN is
not a safe rollback for an active peer installation.
