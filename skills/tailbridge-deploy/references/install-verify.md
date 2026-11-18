# Installation, resume and verification

## Prepare a reproducible install

Read the chosen repository revision. Download `install.sh` and its templates from the same revision; set `INSTALL_BASE_URL` to that revision's directory. The URL is an override for this reproducible deployment, not an extra input users must supply for ordinary installation. When a reviewed local copy exists, compare its SHA-256 with the downloaded installer.

Require Bash on the target and execute the installer with `bash ./install.sh`, preserving the selected role and arguments. Validate required YAML values, certificate paths, port conflicts and SSH/root access first. Preserve strings and quote shell values using a real shell-quoting function; do not `eval` YAML or treat JSON quoting as shell escaping. Credentials go through a protected file or stdin, never an echoed command.

Install only the roles listed in the host's `roles`. For control, pass `SERVER_URL`, all control fields, and the first region's DERP Map fields from `derp` including its port; they only seed a missing Map. For derper, pass `SERVER_URL`, `DERP_HOSTNAME`, `DERP_PORT` and `DERP_CERT_DIR`. For node, pass `SERVER_URL`, the non-empty `node` fields and `NODE_AUTHKEY_FILE`. `acme.email` belongs to Certbot; do not mix `TLS_HOSTNAME`/installer `ACME_EMAIL` with file-certificate mode.

Run each role's `--dry-run` on the target before its install. The dry-run does not check real DNS, valid certificates or service startup. Initial order is certificate placement, control, derper, then node. On a small host, serial Go compilation (`GOMAXPROCS=1`, `GOFLAGS=-p=1`) and a disk-backed `TMPDIR` such as `/var/tmp` avoid build-memory pressure; do not alter maintained component versions as a workaround without investigating.

## Install a node role and a standalone host

A host whose `roles` include `node` joins the tailnet as a device, usually as an exit. Install it last: the control plane and a relay in its Map must already pass their checks. Create its single-use key through the automated path in [node registration](node-registration.md) for `registration_user`, and save the node result as that reference describes. With `ADVERTISE_EXIT_NODE: true`, finish the route approval and internet grant from `docs/OPERATIONS.md`; an advertised exit is not usable before both.

`roles: [control, derp, node]` is a standalone deployment, described in `docs/INSTALL.md`: one host, one certificate covering both public names, `DERP_PORT` 8443 and the ingress of all three roles. Name the machine with the `standalone` role from `docs/NODE_NAMING.md`; the relay and the control entry keep their service names. Before deploying, tell the user that the exit shares the control plane's address, bandwidth and transfer allowance and that nothing on the host is redundant. On a new control plane the management API key and `registration_user` do not exist yet; creating them as `docs/API.md` describes is part of the requested deployment. The node reaches control and the relay through their public names from the same machine: before reporting the host ready, confirm the node is `online`, `tailscale netcheck` on the host reaches the relay, and derper logged no admission failure as described in [Inspect](inspect.md).

## Resume without replacing user state

Compare installed versions, services, configuration and saved results. Complete missing stages instead of reinstalling everything. On an installer conflict, inspect the actual differences. Rerunning control with the same inputs upgrades it and keeps the maintained policy and DERP Map; `--force` replaces only a differing `config.yaml`. Run `tbctl backup` first, and change the policy or Map only through `tbctl publish`.

Headscale uses its packaged systemd service. Derper uses `/etc/derper/derper.conf` through `EnvironmentFile`; its `-c` file is a JSON identity key, not runtime YAML. Preserve that identity. Services must restart without inheriting the operator's shell environment. Keep `verify-client-url-fail-open=false`; repair control reachability instead of disabling verification.

DNS-only changes, Map changes and certificate changes need different follow-up checks. Verify Map periodic reload and client delivery after a Map edit; do not restart the control plane automatically for every update. Certificates copied to service paths need the renewal hook's service restarts.

## Add a relay region to the Map

A new derp host joins the Map only after its own checks pass. Allocate the region ID, code and node name from `docs/NODE_NAMING.md` against the live Map, which is authoritative: read `/etc/headscale/derp.yaml` from the control host. Keep the maintained copy in ignored `local/`, append the region from the relay host's `derp` fields, and publish it through SSH stdin with `sudo tbctl publish derp`. Then confirm with a client's `tailscale debug derp-map` and `tailscale netcheck` that the new region is delivered and reachable. Removing or replacing a region follows the retirement order in `docs/OPERATIONS.md`.

## Verify and record the boundary

- As the service user, run Headscale `configtest` and the installed version's policy check. Confirm the units of the host's roles are active and enabled, `tbctl-backup.timer` is enabled on a control host, service accounts can read certificates, and listeners match the intended ports. Metrics and gRPC stay on loopback; TCP 80 stays closed.
- From outside the server, resolve the real service names and validate trusted TLS/SAN/expiry, control `/health` returning a passing status, an unauthenticated REST request returning 401, and DERP HTTPS responding. Do not use `curl -k` to declare success.
- Verify public STUN with an actual binding request, transaction ID and response validation, or a client `tailscale netcheck`. Derper ignores a bare RFC 5389 request: include SOFTWARE `tailnode` and a final FINGERPRINT, as in the repository's E2E DERP check. An open UDP socket or local listener is insufficient. If the operator's network blocks UDP, report that boundary and test from another authorized vantage point.
- For nodes, reconcile the actual `tailscaled` UDP listener with the attached security groups and host firewall using the repository's network rules. Use `tailscale ping` from an authorized client to record direct versus DERP connectivity; registration and online status alone do not verify direct reachability.
- Verify automatic renewal and deployment separately as described in the TLS reference. Record only checks that ran, with a current timestamp. Keep failed or unperformed checks explicit; do not mark the deployment ready while required checks are failing.

Real-client E2E is a separate, authorized stage: use two clients to verify registration, MagicDNS, allowed and denied traffic, and actual traffic through the intended DERP when direct connectivity is unavailable. STUN or a DERP HTTPS response alone does not prove relay forwarding. Do not create permanent API keys, users or clients merely to make a service check pass. For requested client onboarding, follow [node registration](node-registration.md). Initial `grants: []` denies peer traffic, so successful registration alone does not authorize access.

Report the endpoints, completed checks, renewal status and remaining client verification in a few lines. Avoid writing a historical deployment report into project documentation.
