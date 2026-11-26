# Register devices

Use this stage after node installation or directly for a client joining an existing control plane. Read the repository's `docs/CLIENTS.md` and `docs/API_REFERENCE.md`; use their commands and the deployed API schema. Registration alone needs no provisioning, service restart, or client-side Headscale installation.

## Resolve the request

Read the control URL from the selected deployment input (`common.SERVER_URL`) or saved deployment result. Read the management key from the saved `management_api.key_file`, resolving relative paths against the repository. Keep this key on the management machine.

Use the requested user, an established session choice, or `registration_user` from the selected input. Query the control plane to confirm that user exists; on a control plane deployed in this run that has no users yet, create the input's `registration_user` as `docs/API_REFERENCE.md` describes. Otherwise ask only for unresolved inputs; do not create a user or infer ownership from a hostname. Reuse a known node ID and valid client identity instead of registering again.

A request to enroll an identified device authorizes that enrollment; reuse existing authorization rather than asking again. A question about the design or an instruction to edit this skill does not authorize live registration. Run client commands only on the intended device, not implicitly on the operator's computer.

## Choose the registration path

- **Interactive client:** use the complete Auth ID supplied by the user or registration page. Send `POST /api/v1/auth/register` with `user` as the existing username and `authId` as that request ID, following the repository's approval instructions. The page's `headscale auth register` command is for the administrator; REST performs this step without SSH. If the request expired, have the client initiate login again and provide a fresh Auth ID.
- **Automated installation:** create a short-lived, single-use preauthkey for the existing user's numeric ID, following the repository's preauthkey instructions. Use a persistent node for normal installations; use an ephemeral node only for a temporary test. Transfer only the node authkey to the target as a protected file and pass its path as `NODE_AUTHKEY_FILE` to the node installer. Remove temporary credential files and revoke an unused key when abandoning the attempt.

Keep Auth IDs and node authkeys distinct from the management API key. Do not put any of them in skill files, documentation, reports, or logs. Client identity is saved after registration; routine restarts do not need another key or repeated registration parameters.

Do not blindly retry a registration after a timeout or interrupted response. Reconcile control-plane and target-client state first; a hostname alone does not prove identity. If the outcome cannot be determined, report that uncertainty instead of creating a replacement identity.

## Verify and hand off

1. Read the resulting node by its returned ID. Confirm the intended user, name and tailnet addresses without printing nested credentials.
2. Check `online`; allow a short bounded wait for the client to connect. A successful registration with an offline client is only **registered**. Report **online** only after observing it, and keep traffic verification separate.
3. Save a current non-secret snapshot to `local/nodes/<device>.yaml` through the skill's `write_state.py`: `status`, `server_url`, `node_id`, `name`, `user`, `ip_addresses`, `online`, `verified_at`. Reuse an existing matching file and preserve valid unrelated fields; never overwrite another device's record just because names match. Use a safe filename component, not an unchecked hostname as a path.
4. Give the client the next applicable check from `docs/CLIENTS.md`. Keep policy grants, exit-route approval and exit selection in the repository's existing operations flow. Registration does not itself grant peer access or authorize changing the operator's routes; only run `install.sh test --exit-node` when that comparison is requested.

Return the node name, owner, tailnet address and observed registration/online status. Future onboarding skills can reuse this reference and result without reimplementing cloud provisioning or credential storage.
