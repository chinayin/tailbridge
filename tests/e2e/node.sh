#!/usr/bin/env bash
# Run a tailnet node with SSH bound to its tailnet address
set -euo pipefail

: "${TB_NODE_ROLE:?TB_NODE_ROLE is required}"
: "${SERVER_URL:?SERVER_URL is required}"
: "${ADVERTISE_EXIT_NODE:?ADVERTISE_EXIT_NODE is required}"
[[ "${ADVERTISE_EXIT_NODE}" == true || "${ADVERTISE_EXIT_NODE}" == false ]] || {
  echo "Error: ADVERTISE_EXIT_NODE must be true or false" >&2; exit 1;
}
[[ "${TB_NODE_ROLE}" == client || "${TB_NODE_ROLE}" == server ]] || {
  echo "Error: invalid TB_NODE_ROLE" >&2; exit 1;
}
[[ -s /var/lib/tailscale/tailscaled.state || -s /run/secrets/tailscale_authkey ]] || {
  echo "Error: no node state; provide a bootstrap auth key for first registration" >&2; exit 2;
}
had_state=false
[[ ! -e /var/lib/tailscale/tailscaled.state ]] || had_state=true
if [[ "${had_state}" == true && ! -s /var/lib/tailscale/tailscaled.state ]]; then
  echo 'Error: existing node state is empty; explicit recovery is required' >&2
  exit 2
fi

if [[ "${ADVERTISE_EXIT_NODE}" == true ]]; then
  # Route the simulated Internet through the next hop without advertising a subnet
  ip route replace "${TB_EXIT_PROBE_IPV4_SUBNET:?Required HTTP network}" \
    via "${TB_EXIT_PROBE_LINK_IPV4_ADDRESS:?Required HTTP next hop}"
  ip -6 route replace "${TB_EXIT_PROBE_IPV6_SUBNET:?Required HTTP network}" \
    via "${TB_EXIT_PROBE_LINK_IPV6_ADDRESS:?Required HTTP next hop}"
fi

update-ca-certificates >/dev/null
mkdir -p /var/lib/tailscale /var/run/tailscale /run/sshd /run/tailbridge
children=()
# Called indirectly by the EXIT trap
# shellcheck disable=SC2329
cleanup() {
  trap - EXIT TERM INT
  if [[ ${#children[@]} -gt 0 ]]; then
    kill "${children[@]}" 2>/dev/null || true
    wait "${children[@]}" 2>/dev/null || true
  fi
}
trap 'cleanup' EXIT
trap 'exit 143' TERM
trap 'exit 130' INT

tailscaled --state=/var/lib/tailscale/tailscaled.state \
  --socket=/var/run/tailscale/tailscaled.sock >/var/log/tailscaled.log 2>&1 &
children+=("$!")
for ((i = 0; i < 100; i++)); do
  [[ -S /var/run/tailscale/tailscaled.sock ]] && break
  kill -0 "${children[0]}" 2>/dev/null || { echo "Error: tailscaled exited" >&2; exit 1; }
  sleep 0.2
done
[[ -S /var/run/tailscale/tailscaled.sock ]] || { echo "Error: tailscaled startup timed out" >&2; exit 1; }

backend="$(python3 /app/node_status.py)"
if [[ "${had_state}" == true ]]; then
  case "${backend}" in
    Running) ;;
    Stopped) timeout 45 tailscale up ;;
    *) echo 'Error: existing node identity needs explicit reauthentication; state preserved' >&2; exit 2 ;;
  esac
else
  auth_args=(--auth-key=file:/run/secrets/tailscale_authkey)
  [[ -z "${NODE_HOSTNAME:-}" ]] || auth_args+=(--hostname="${NODE_HOSTNAME}")
  tailscale up --login-server="${SERVER_URL}" \
    "${auth_args[@]}" \
    --accept-dns=true --accept-routes=false --timeout=45s
fi

timeout 45 tailscale set "--advertise-exit-node=${ADVERTISE_EXIT_NODE}"

if [[ "${TB_NODE_ROLE}" == server ]]; then
  # Bind mounts retain the host UID; sshd requires root-owned authorized keys
  install -o root -g root -m 600 /etc/tailbridge/ssh/authorized_keys /run/tailbridge/authorized_keys
  tail_ip="$(tailscale ip -4)"
  cat > /run/tailbridge/sshd.conf <<EOF
Port 22
ListenAddress ${tail_ip}
HostKey /run/secrets/ssh_host_key
AuthorizedKeysFile /run/tailbridge/authorized_keys
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
PidFile /run/sshd/pid
StrictModes yes
PrintMotd no
LogLevel ERROR
EOF
  /usr/sbin/sshd -D -e -f /run/tailbridge/sshd.conf &
  children+=("$!")
fi

echo "Node ready: ${NODE_HOSTNAME:-$(hostname)}" >&2
wait -n "${children[@]}"
exit 1
