# Inspect the deployment

A read-only pass over every host in `local/`. It reports differences and failures; it never repairs, restarts, publishes or creates anything. Repairs follow the other references after the user decides.

## Collect

1. **Inventory:** read every `local/deploy-*.yaml`, `local/aws/*.yaml`, `local/dns.yaml`, `local/acme.yaml` and the maintained Map in `local/`. Each host's `roles` decides which checks apply.
2. **Control:** over SSH run `sudo tbctl status --json` on the control host. It covers the control service, `/health` and certificate, policy, newest backup, disk, every Map node's HTTPS, certificate and STUN, and each registered node; thresholds are in the repository's `docs/INSTALL.md`. Exit status `2` means a check failed; the JSON is still complete. Compare its `regions` with the maintained Map and its `nodes` with `local/nodes/`.
3. **Each relay host:** over SSH check `systemctl is-active derper`, `systemctl is-enabled certbot.timer`, the certificate expiry of `/etc/letsencrypt/live/<acme.cert_name>`, and derper's admission failures in the last 24 hours:

   ```sh
   sudo journalctl -u derper --since -24h --no-pager | grep ' rejected: ' | grep -cv ' not allowed$'
   ```

   The count is the result; `grep` exits 1 when it is 0. Lines ending in `not allowed` are unregistered keys being refused as intended. Any other `rejected:` line means derper could not get an answer from the control plane's `/verify`, so clients could not connect.
4. **Cloud (read-only):** for Lightsail, `get-instance-state`, `get-static-ip` (still attached), `get-instance-port-states` against the recorded rules (TCP 80 closed, SSH limited to `ssh_source_cidrs`), and month-to-date `NetworkIn` + `NetworkOut` from `get-instance-metric-data` with `--period 86400 --statistics Sum` against `transfer_per_month_gb`; both directions count towards the allowance. For EC2, `describe-instances` state and the recorded security group rules.
5. **DNS and tokens:** list the recorded names with `cf dns records list` and compare type, content and `proxied: false` with `local/dns.yaml`; flag unexpected AAAA records. `cf user tokens get` for each recorded token: active, and `request_ip` equal to its host's static address.

Use the host's recorded SSH settings with strict host key checking. A host that cannot be reached is a failure to report, not a reason to change its known_hosts entry.

## Judge and report

Use `tbctl status` levels as they are. For the other checks: a stopped instance, detached static IP, inactive derper, inactive renewal timer, admission failures, DNS or firewall drift, or an inactive token is `fail`; certificates follow the `tbctl status` thresholds; transfer above 80% of the allowance is `warn`.

Report a topology table (control, regions with relay hosts and ports, nodes with online state) followed only by `warn` and `fail` items, each with the host and the observed value. Save the latest result to `local/inspect.yaml` with [scripts/write_state.py](../scripts/write_state.py): time, overall status, per-host checks and the topology. Replace the previous result instead of accumulating history, and exclude keys, tokens and node registration details.

## Run daily

Schedule the agent on the operator machine, where `local/`, SSH keys and the cloud CLIs are available, for example once a day with a non-interactive prompt to inspect TailBridge with this skill. A cloud-hosted scheduler lacks those credentials. Notify only when the overall status is not `ok`.
