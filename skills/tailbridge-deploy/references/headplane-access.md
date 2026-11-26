# Headplane access from the operator's computer

Read this when handing over or troubleshooting access to an already deployed Headplane. Read `ssh.host`, `ssh.user` and `ssh.private_key_file` from the deployment YAML; never embed their actual values in the skill.

## Open the local SSH tunnel

Check the actual Headplane service/container and host listener first. The usual remote endpoint is `127.0.0.1:3000`; for Docker, use the published host port. If Headplane is absent, report that it is not deployed instead of claiming a tunnel makes it available. Port forwarding needs the existing SSH ingress rule, not a public WebUI port.

Build a ready-to-run command for the user, safely substituting their SSH values into this pattern. Use local port 3300 by default; `WEBUI_PORT` is the verified remote host port:

```sh
LOCAL_PORT=3300
ssh -N -T \
  -o IdentitiesOnly=yes -o StrictHostKeyChecking=yes \
  -o ExitOnForwardFailure=yes \
  -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  -i "$SSH_KEY" \
  -L "127.0.0.1:${LOCAL_PORT}:127.0.0.1:${WEBUI_PORT}" \
  "${SSH_USER}@${SSH_HOST}"
```

Use the verified `known_hosts` file; add `UserKnownHostsFile` when the recorded pin is outside the default file. Run this on the operator's computer, without sudo. Keep the terminal open; no remote prompt is expected. Ctrl-C closes the tunnel. Binding explicitly to loopback keeps access local. See [OpenSSH local forwarding](https://man.openbsd.org/ssh#L).

For the usual loopback HTTP setup, open `http://127.0.0.1:3300/admin`. Configure the browser-facing `server.base_url: http://127.0.0.1:3300` and `server.cookie_secure: false`; the remote listener can remain on port 3000. Check existing configuration and login redirects before using the new local port. HTTPS deployments retain their configured scheme and secure cookies.

If the port is occupied, identify the existing listener before choosing another port. If SSH reports `administratively prohibited`, inspect the server's forwarding policy. A tunnel opening successfully does not prove the destination is reachable: request `/admin` through it and verify the page or login redirect before reporting access as tested.

## Display the API key and log in

Read the local key path from the control host's `deployment-<machine>.yaml` at `management_api.key_file`; the default is `local/headscale-api.key`. Reuse a valid key for this control server and keep its file at mode 600. If issuance or renewal is needed, follow the repository's `docs/API_REFERENCE.md` under the user's authorization, and save only the path, ID and expiry in deployment results. Treat the key as opaque; do not assume the legacy dot-separated format.

Give the user the local file link and a command they can run in their own terminal. With the default path, from the project directory:

```sh
cat local/headscale-api.key
```

On macOS, copying avoids displaying the secret in terminal output:

```sh
pbcopy < local/headscale-api.key
```

Replace these paths when a different key file is configured. Do not execute the display command in assistant tool output or put the secret in the skill, chat, screenshots or URL.

1. Keep the SSH tunnel running and open the local `/admin` page.
2. Choose API Key login, paste the complete Headscale management API key, and sign in. A node registration authkey will not work.
3. Confirm the dashboard can read the intended control server. Respect a viewing-only request: do not create, edit, expire or delete resources while verifying access.

API key sessions have full administrative access and bypass Headplane's role system. Read-only configuration files do not enforce a read-only UI. State this boundary when the user wants viewing only; do not describe API key login as an enforced read-only mode. See [Headplane API key sessions](https://headplane.net/features/sso#api-key-sessions).

The handoff should contain the forwarding command, browser URL, key file/display or copy command, expiry and whether page access/login were actually tested. Keep real environment values in that deployment's handoff, never in this reusable reference.
