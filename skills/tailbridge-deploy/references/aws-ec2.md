# AWS EC2 host

Use EC2 only when the host needs Ubuntu 26.04, arm64, will stay stopped most of the time, or the user asks for it; the default AWS flow is [Lightsail](aws-lightsail.md). An existing SSH target does not need to be reprovisioned.

Pass `--profile "$PROFILE" --region "$REGION" --no-cli-pager` from the input's `host` section on every command.

## Locate before creating

- Check `aws sts get-caller-identity`. Reconcile saved resource IDs with `describe-instances`, addresses, security groups and volumes in that account and region.
- Resolve the official image from SSM instead of embedding an AMI ID: Ubuntu 26.04 `/aws/service/canonical/ubuntu/server/resolute/stable/current/$ARCH/hvm/ebs-gp3/ami-id` (initial user `ubuntu`). Check owner, architecture and state with `describe-images`, and read the installer's supported OS and architectures.
- Choose the smallest instance type that fits, for example 1 GB for one role and 2 GB for control and derp on one host (a Graviton type for arm64). Upgrade later when monitoring shows pressure.
- If creation is not yet approved, prepare the concrete region, instance type, image, encrypted disk, public address, ingress and current cost estimate, and obtain approval for that plan once; existing approval carries forward.
- Reuse the configured SSH key. When a new key is requested, use the available `gen-ssh-key` skill and import only its public key. Preserve existing private keys and record their local paths.

## Create the approved resources

Use a stable EC2 client token and project tags (`ManagedBy=tailbridge`). Save returned identifiers before subsequent steps. Before retrying an uncertain create, query its outcome by client token; do not create a second instance, Elastic IP or security group just because a command timed out.

Use encrypted gp3 storage, IMDSv2 and standard CPU credits for burstable types. Disk retention on termination follows the approved plan. Allocate an Elastic IP for a host that will be kept, so DNS stays stable.

Create a dedicated security group per host. Build ingress from the checked-out repository's `docs/INSTALL.md` role rules ([published guide](https://github.com/chinayin/tailbridge/blob/main/docs/INSTALL.md)). For public cloud nodes, include the recommended UDP direct-connection ingress using the actual `tailscaled` listening port, while respecting explicitly restricted ingress plans. SSH is restricted to `ssh_source_cidrs`. Adding a current address preserves other authorized sources; removing them is a separate change. Do not expose metrics, gRPC or a management UI. Check the host firewall as well as the security group.

Wait for EC2 status checks and cloud-init completion. Verify the SSH fingerprint from `get-console-output --latest` or an already trusted channel before pinning it in `known_hosts`; `ssh-keyscan` alone is discovery, not authentication. Use `BatchMode=yes`, `IdentitiesOnly=yes` and `StrictHostKeyChecking=yes` afterwards.

Pass the first-boot script from SKILL.md to `run-instances` as `--user-data`; it sets the OS hostname, which the AWS `Name` tag does not. Confirm `hostname` and `sudo sshd -T` (`permitrootlogin no`, `passwordauthentication no`) after cloud-init completes. Service DNS names do not require renaming a shared host.

Record in `local/aws/<machine>.yaml`: resource IDs, region, zone, instance type, address, SSH user and key path, known_hosts path and the verified host key fingerprint. On failure, preserve created resources and record the incomplete stage. Delete resources only when the user authorizes it; never terminate instances, release addresses or remove volumes as automatic cleanup.
