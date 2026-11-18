---
name: tailbridge-deploy
description: Create a cloud host (AWS Lightsail by default, or EC2) and deploy or resume TailBridge control and DERP relays from per-host YAML, add a relay region to the DERP Map, register client devices through the management API, inspect the whole deployment read-only (topology, control, relays, nodes, cloud, DNS and tokens), or access the deployed Headplane UI over SSH. Use when the user wants to deploy, extend, resume or inspect TailBridge (including a daily check), add a DERP region, onboard a device, or open Headplane.
---

# TailBridge Deploy

Deploy from user-maintained YAML and reconcile the actual host before changing it. Use the repository installer; this skill coordinates hosts, certificates, the DERP Map and verification around it.

For device registration alone, go directly to [Register devices](references/node-registration.md). For a health check or daily inspection, go directly to [Inspect](references/inspect.md); it changes nothing. For infrastructure, follow the stages below.

## Start with the current configuration

Read the chosen per-host input, such as `local/deploy-<machine>.yaml`, plus the repository's `docs/INSTALL.md`, `docs/NODE_NAMING.md`, `docs/OPERATIONS.md` and installer. Read component versions and supported platforms from the installer and `docs/INSTALL.md`.

For a new host, copy [assets/deploy.example.yaml](assets/deploy.example.yaml). Each host has its own input and certificate; `roles` lists what runs on it. The installer does not parse YAML: pass `common` + `control` + the first region's `derp` fields to control, and `common` + the `derp` listener fields to derper, as environment variables. `host`, `ssh` and `acme` configure this workflow.

Reconcile saved results with live resources before provisioning. Inspect CLI authentication without dumping credentials; missing `cloudflare.ini` does not imply missing Cloudflare access. Reuse existing authorization and ask only for unresolved inputs or additional scope. Skill creation and plan review do not authorize live deployment: creating paid cloud resources needs the user's approval of the concrete plan and price.

## Keep the repository free of deployment data

Real domains, addresses, account and resource IDs, certificate names, key paths, tokens and user names belong only in ignored `local/` (directories 700, files 600). Never write them into this skill, its templates, repository documentation or commit messages; use placeholders such as `<machine>` instead.

## Execute the applicable stages

1. **Host:** reuse the configured SSH target. To create or change an AWS host, default to [Lightsail](references/aws-lightsail.md) with Debian 13 and the smallest plan that fits; use [EC2](references/aws-ec2.md) only for the cases listed there. Verify the SSH host key and administrative access before uploading credentials.
2. **DNS and TLS:** follow [references/dns-tls.md](references/dns-tls.md). Apply the role-specific network rules from `docs/INSTALL.md`. Use file certificates and DNS-01 renewal; keep TCP 80 closed. Public service names must be outside `BASE_DOMAIN`. Preserve explicit domain choices; resolve a conflict before issuance.
3. **Install or resume:** follow [references/install-verify.md](references/install-verify.md). Pin installer and templates to the same source revision. Run each role's dry-run, then install only missing or intended changes. A differing configuration is a review point, not a reason to append `--force`.
4. **Add a relay region:** a new derp host does not register itself. After it passes its checks, add its region to the control plane's maintained Map and publish it with `tbctl publish derp`, as described in the install reference.
5. **Register devices:** when client onboarding is included, follow [node registration](references/node-registration.md). This stage also works independently against an existing control plane.
6. **Renew and verify:** finish the certificate deployment hook, test renewal and hook execution separately, then check public services. Report service readiness separately from real-client E2E.

Preserve Headscale's database, Noise key, policy, complete DERP Map and derper identity. Run `tbctl backup` before changing an existing control plane.

## Access an installed WebUI

When Headplane access is requested, follow [references/headplane-access.md](references/headplane-access.md). Give the local SSH forwarding command, browser URL, local API key display/copy command and login steps. Verify Headplane is installed before claiming the page is available. This access procedure does not add a `webui` installer role.

## Save current results

Keep inputs and results in ignored `local/`. `deploy-<machine>.yaml` is input; record observed evidence in:

| Result | Retain |
| --- | --- |
| `aws/<machine>.yaml` | Provider, resource IDs or names, region, plan and price, static address, SSH user, key path and verified host fingerprint |
| `dns.yaml` | Zone ID, record IDs, names, values and proxy status |
| `acme.yaml` | Renewal token metadata, credential file path, certificate lineage per host and expiry |
| `deployment.yaml` | Source revision, installer checksum, stage, checks, pending verification and management API key path/expiry |
| `inspect.yaml` | Latest inspection: time, overall status, per-host checks and topology |
| `nodes/<device>.yaml` | Control URL, node ID, name, user, tailnet addresses, registration/online status and verification time |

Invoke [scripts/write_state.py](scripts/write_state.py) through Python with a JSON object on stdin and a `.yaml` destination. It replaces a complete snapshot atomically with mode 600. Retain valid fields, exclude secrets, and save each completed stage for recovery. Keep current results; do not accumulate reports in `docs/` or treat old checks as fresh evidence.

Helpers require Python 3 with PyYAML; reuse the project virtual environment. Resolve paths from this skill directory. [scripts/render_cert_hook.py](scripts/render_cert_hook.py) generates a hook; neither helper performs remote operations. `make test` runs their tests.
