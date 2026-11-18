# Cloudflare DNS and file certificates

## Resolve names and existing access

Read `SERVER_URL` as an HTTPS origin, derive its host, and validate `BASE_DOMAIN` as a DNS suffix without a scheme. The control and DERP hosts must neither equal nor be children of `BASE_DOMAIN`. When names are missing, use the repository's naming convention and the actual hosted zone to propose them; preserve approved choices in the YAML. Do not guess a zone by removing a fixed number of labels.

Discover `cf` on PATH, inspect `cf auth whoami`, then locate the correct zone. Use `cf cli search` and command help to confirm the installed CLI's syntax; search terms describe operations, not private domains or identifiers. CLI stdout is JSON; pagination notices may be on stderr.

List exact-name A/AAAA/CNAME records before changes. Reuse matching records, update an authorized existing record by ID, and stop on conflicting ownership or an unapproved target change. Service records use DNS only (`proxied: false`). Verify authoritative DNS and public resolution; account for stale AAAA records rather than silently deleting them. MagicDNS's base domain does not need a public A record for this deployment.

## Prepare unattended renewal

Reuse a working renewal credential. If missing and issuance is authorized, use the authenticated `cf` CLI to create a dedicated API token scoped to the actual zone, with DNS Edit and Zone Read for this workflow. Discover permission-group IDs at runtime. Restrict its source IP to the target's observed stable outbound address when available. A dual-stack host such as Lightsail may reach the API over its non-static IPv6 address, which the token rejects: make the target prefer IPv4 (`precedence ::ffff:0:0/96 100` in `/etc/gai.conf`) rather than allowing that address. If the CLI cannot create it, request the missing scoped credential only then. Keep the CLI's broad OAuth credential on the operator machine.

Capture token creation output without printing it. Store only `dns_cloudflare_api_token = ...` in the configured local INI (600), then transfer it through SSH stdin into `/etc/letsencrypt/secrets/cloudflare.ini` (root:root 600, parent 700). Store token metadata separately, excluding its secret value. Do not pass token values through command arguments or logs. The INI format is required by this Certbot plugin; YAML is for deployment inputs and results. See [Cloudflare token scoping](https://developers.cloudflare.com/fundamentals/api/get-started/create-token/) and [Certbot's credential format](https://certbot-dns-cloudflare.readthedocs.io/en/stable/).

Use `acme.email`; if empty, an authenticated account's verified email can be proposed as the contact and saved with the user's authorization. Install distribution `certbot` and `python3-certbot-dns-cloudflare` packages on the target. Check existing lineages and renewal configuration before issuing. Avoid repeated production issuance while troubleshooting; use staging for experiments.

Issue one certificate per host covering the public names of its roles, named by `acme.cert_name`. After deriving and safely quoting these shell variables from the YAML, run on the target (one `-d` per role name; a relay-only host has just `DERP_HOSTNAME`):

```sh
sudo certbot certonly --non-interactive --agree-tos \
  --email "$ACME_EMAIL" --cert-name "$CERT_NAME" --key-type ecdsa \
  --dns-cloudflare \
  --dns-cloudflare-credentials /etc/letsencrypt/secrets/cloudflare.ini \
  --dns-cloudflare-propagation-seconds 30 \
  -d "$CONTROL_HOST" -d "$DERP_HOSTNAME"
```

Existing certificates supplied by the user need SAN, expiry and key-pair validation; retain their renewal provider. Do not replace them with ACME merely to follow this example.

## Publish to the host's services

Before initial installation, copy the full chain and private key to the paths of each role in `roles`: the YAML's control paths, and `<DERP_CERT_DIR>/<DERP_HOSTNAME>.crt` / `.key`. Check on the target that these are distinct files, including resolved symlink paths. Create traversable parent directories; stage private keys as root-only until the installer grants the service group access. Do not relax access to the entire Certbot directory.

After the host's service accounts exist, generate its renewal hook locally:

```sh
python3 "$SKILL_DIR/scripts/render_cert_hook.py" local/deploy-<machine>.yaml \
  > local/certbot-deploy-hook-<machine>.sh
```

`SKILL_DIR` is the resolved skill directory; `python3` must have PyYAML. The lineage defaults to `/etc/letsencrypt/live/<acme.cert_name>`; pass `--lineage` for a different existing certificate. Upload the hook to `/etc/letsencrypt/renewal-hooks/deploy/tailbridge` as root with mode 750. It guards the lineage, replaces only the files of the host's roles, gives private keys mode 640 and the correct service group, then restarts those services.

Enable/check `certbot.timer`. Run a renewal dry-run for the chosen certificate, then explicitly exercise the hook with the current production lineage in `RENEWED_LINEAGE`. A successful dry-run alone does not prove hook execution. Compare deployed files with the lineage, verify each service user can read its key, and check renewed public TLS after restart. Confirm challenge TXT cleanup. See [Certbot renewal and deploy hooks](https://eff-certbot.readthedocs.io/en/stable/using.html#renewing-certificates).

Do not open TCP 80 for DNS-01. Harmless package deprecation output is not an issuance failure: use exit status and certificate checks, and keep the distribution's compatible package set unless an actual failure requires a change.
