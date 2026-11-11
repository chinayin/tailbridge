#!/usr/bin/env bash
# Start container services after installation through the root installer
set -euo pipefail
role="${1:?role is required}"
shift
args=(--role "${role}" "$@")
[[ -z "${TB_HEADSCALE_VERSION:-}" ]] || args+=(--headscale-version "${TB_HEADSCALE_VERSION}")
[[ -z "${TB_TAILSCALE_VERSION:-}" ]] || args+=(--tailscale-version "${TB_TAILSCALE_VERSION}")

install_component() {
    # Use the download proxy only in the installer subprocess
    env PROXY="${TB_INSTALL_PROXY:-}" NO_PROXY=localhost,127.0.0.1,::1,installer-files \
        no_proxy=localhost,127.0.0.1,::1,installer-files sh /app/install.sh "${args[@]}"
}

case "${role}" in
    control)
        install -d -m 755 /run/tailbridge
        install -m 644 /run/secrets/control.crt /run/tailbridge/control.crt
        install -m 600 /run/secrets/control.key /run/tailbridge/control.key
        # Preserve the DERP map and policy on restart; install in fresh containers
        if ! command -v headscale >/dev/null 2>&1; then install_component; fi
        chgrp headscale /run/tailbridge/control.key
        chmod 640 /run/tailbridge/control.key
        exec setpriv --reuid=headscale --regid=headscale --init-groups headscale serve
        ;;
    node)
        install_component
        exec bash /app/node.sh
        ;;
    derp)
        cert_dir="${DERP_CERT_DIR:-/etc/derper/certs}"
        # Copy read-only secrets before the installer grants service access
        install -d -m 755 "${cert_dir}"
        install -m 644 "/run/secrets/derper/${DERP_HOSTNAME}.crt" "${cert_dir}/${DERP_HOSTNAME}.crt"
        install -m 600 "/run/secrets/derper/${DERP_HOSTNAME}.key" "${cert_dir}/${DERP_HOSTNAME}.key"
        # Reconcile the account and service on restart; matching binaries are reused
        install_component
        # Read the persisted service settings after installation
        # shellcheck disable=SC1091
        source /etc/derper/derper.conf
        exec setpriv --reuid=derper --regid=derper --init-groups \
            derper -hostname="${DERP_HOSTNAME}" -a=":${DERP_PORT}" \
            -http-port=-1 -stun=true -stun-port=3478 -certmode=manual -certdir="${DERP_CERT_DIR}" \
            -c=/var/lib/derper/derper.key -verify-client-url="${SERVER_URL%/}/verify" \
            -verify-client-url-fail-open=false
        ;;
    *) echo 'Error: unknown role' >&2; exit 1 ;;
esac
