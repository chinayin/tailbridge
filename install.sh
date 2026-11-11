#!/bin/sh
# Install official components on the target; read templates from files/
set -eu

VERSION=0.5.1
INSTALL_BASE_URL=${INSTALL_BASE_URL:-https://raw.githubusercontent.com/chinayin/tailbridge/main}
DERP_PORT=${DERP_PORT:-443}
DERP_CERT_DIR=${DERP_CERT_DIR:-/etc/derper/certs}
HEADSCALE_VERSION=0.29.7
TAILSCALE_VERSION=1.104.5
# Maintainers pin the verified Go toolchain; no CLI override
GO_VERSION=1.27.4
TB_PREFIX=${TB_PREFIX:-}
TB_OFFLINE=${TB_OFFLINE:-0}
ROLE='' FORCE=0 DRY_RUN=0 TMP='' STAGED='' POLICY_GUARD='' CHANGED=0 PACKAGE_CHANGED=0 HAD_STATE=0
CONFIG_PENDING=0 CONFIG_FILES=''

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
missing() { printf 'Error: %s\n' "$*" >&2; exit 2; }
warn() { printf 'Warning: %s\n' "$*" >&2; }

usage() {
    cat <<'EOF'
Usage: install.sh --role control|derp|node [options]
  --headscale-version X.Y.Z   Override the Headscale version
  --tailscale-version X.Y.Z   Override the Tailscale and derper version
  --force                    Replace differing configuration (never identity)
  --dry-run                  Show URLs and paths without making changes
  --version                  Show the installer version
  -h, --help                 Show this help
Control environment: SERVER_URL,
BASE_DOMAIN, DERP_REGION_ID, DERP_REGION_CODE, DERP_REGION_NAME, DERP_NODE_NAME,
DERP_HOSTNAME; DERP_PORT defaults to 443. TLS_HOSTNAME and
ACME_EMAIL for ACME, or TLS_CERT_PATH and TLS_KEY_PATH for file certificates.
DERP_IPV4 is optional. The Go toolchain version is maintained in this script.
DERP environment: SERVER_URL, DERP_HOSTNAME;
DERP_PORT defaults to 443, DERP_CERT_DIR to /etc/derper/certs.
Provide <DERP_HOSTNAME>.crt and .key before installing. systemd enables derper;
without systemd the caller must start the relay using /etc/derper/derper.conf.
INSTALL_BASE_URL overrides the template source (defaults to this repository's main).
Node environment: SERVER_URL; NODE_AUTHKEY for first registration.
NODE_HOSTNAME is optional; otherwise Tailscale uses the local hostname.
ADVERTISE_EXIT_NODE=true enables IP forwarding and advertises an exit node;
unset or false preserves existing settings. Route approval is separate.
Without systemd the caller must start, register and advertise the node.
Exit codes: 0 success; 1 invalid argument/runtime failure/overwrite refused;
2 missing prerequisite or identity requiring explicit reauthentication.
EOF
}

parse_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --role|--headscale-version|--tailscale-version)
                [ "$#" -ge 2 ] || die "missing value for ${1}"
                case "$1" in
                    --role) ROLE=$2 ;;
                    --headscale-version) HEADSCALE_VERSION=$2 ;;
                    --tailscale-version) TAILSCALE_VERSION=$2 ;;
                esac
                shift 2 ;;
            --force) FORCE=1; shift ;;
            --dry-run) DRY_RUN=1; shift ;;
            --version) printf 'tailbridge-install %s\n' "${VERSION}"; exit 0 ;;
            -h|--help) usage; exit 0 ;;
            --) shift; [ "$#" -eq 0 ] || die 'unexpected positional argument' ;;
            *) usage >&2; die "unknown argument: ${1}" ;;
        esac
    done
    case "${ROLE}" in control|derp|node) ;; *) die 'choose --role control, derp or node' ;; esac
    for version in "${HEADSCALE_VERSION}" "${TAILSCALE_VERSION}" "${GO_VERSION}"; do
        printf '%s\n' "${version}" | LC_ALL=C grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' || die 'versions must use X.Y.Z'
    done
}

detect_arch() {
    [ "$(uname -s)" = Linux ] || missing 'Linux is required'
    command -v apt-get >/dev/null 2>&1 || missing 'apt-get is required'
    command -v dpkg >/dev/null 2>&1 || missing 'dpkg is required'
    ARCH=$(dpkg --print-architecture)
    case "${ARCH}" in amd64|arm64) ;; *) missing 'only amd64 and arm64 are supported' ;; esac
}

require_var() { [ -n "$2" ] || missing "${1} is required"; }
reject_value() {
    # Reject characters unsafe for quoted YAML scalars or sed replacements
    case "$1" in *\|*|*\&*|*\\*|*\"*|*\'*|*"\${"*) die 'unsafe replacement value' ;; esac
    if printf '%s' "$1" | LC_ALL=C grep -q '[[:cntrl:]]'; then die 'control character in replacement value'; fi
    case "$1" in *'
'*) die 'newline in replacement value' ;; esac
}

validate_inputs() {
    case "${ADVERTISE_EXIT_NODE:-false}" in
        true) [ "${ROLE}" = node ] || die 'ADVERTISE_EXIT_NODE=true requires --role node' ;;
        false) ;;
        *) die 'ADVERTISE_EXIT_NODE must be true or false' ;;
    esac
    case "${ROLE}" in
        control)
            require_var SERVER_URL "${SERVER_URL:-}"
            require_var BASE_DOMAIN "${BASE_DOMAIN:-}"
            require_var DERP_REGION_ID "${DERP_REGION_ID:-}"
            require_var DERP_REGION_CODE "${DERP_REGION_CODE:-}"
            require_var DERP_REGION_NAME "${DERP_REGION_NAME:-}"
            require_var DERP_NODE_NAME "${DERP_NODE_NAME:-}"
            require_var DERP_HOSTNAME "${DERP_HOSTNAME:-}"
            if [ -n "${TLS_HOSTNAME:-}" ]; then
                require_var ACME_EMAIL "${ACME_EMAIL:-}"
                [ -z "${TLS_CERT_PATH:-}${TLS_KEY_PATH:-}" ] || die 'TLS modes are mutually exclusive'
            else
                require_var TLS_CERT_PATH "${TLS_CERT_PATH:-}"
                require_var TLS_KEY_PATH "${TLS_KEY_PATH:-}"
                [ -z "${ACME_EMAIL:-}" ] || die 'ACME email requires TLS hostname'
            fi
            for value in "${SERVER_URL}" "${BASE_DOMAIN}" "${DERP_HOSTNAME}" \
                "${DERP_REGION_ID}" "${DERP_REGION_CODE}" "${DERP_REGION_NAME}" "${DERP_NODE_NAME}" \
                "${DERP_IPV4:-}" "${DERP_PORT}" "${ACME_EMAIL:-}" \
                "${TLS_HOSTNAME:-}" "${TLS_CERT_PATH:-}" "${TLS_KEY_PATH:-}"; do
                reject_value "${value}"
            done
            printf '%s\n' "${DERP_REGION_ID}" | LC_ALL=C grep -Eq '^[1-9][0-9]{0,4}$' || die 'invalid DERP region ID'
            [ "${DERP_REGION_ID}" -le 65535 ] || die 'DERP region ID must be between 1 and 65535'
            printf '%s\n' "${DERP_REGION_CODE}" | LC_ALL=C grep -Eq '^[a-z][a-z0-9-]{0,31}$' || die 'invalid DERP region code'
            printf '%s\n' "${DERP_NODE_NAME}" | LC_ALL=C grep -Eq '^[a-z][a-z0-9-]{0,47}$' || die 'invalid DERP node name'
            printf '%s\n' "${DERP_REGION_NAME}" | LC_ALL=C grep -q '[^[:space:]]' || die 'DERP region name must not be blank'
            case "${DERP_PORT}" in *[!0-9]*|'') die 'invalid DERP port' ;; esac
            [ "${DERP_PORT}" -ge 1 ] && [ "${DERP_PORT}" -le 65535 ] || die 'invalid DERP port' ;;
        derp)
            require_var SERVER_URL "${SERVER_URL:-}"
            require_var DERP_HOSTNAME "${DERP_HOSTNAME:-}"
            for value in "${SERVER_URL}" "${DERP_HOSTNAME}" "${DERP_CERT_DIR}"; do
                reject_value "${value}"
                case "${value}" in *\$*|*%*|*\`*) die 'expansions are not allowed in DERP settings' ;; esac
            done
            printf '%s\n' "${SERVER_URL}" | LC_ALL=C grep -Eq '^https://[a-zA-Z0-9.-]+(:[0-9]+)?/?$' ||
                die 'SERVER_URL must be an HTTPS origin'
            SERVER_URL=${SERVER_URL%/}
            printf '%s\n' "${DERP_HOSTNAME}" | LC_ALL=C grep -Eq '^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$' ||
                die 'invalid DERP hostname'
            case "${DERP_CERT_DIR}" in /*) ;; *) die 'DERP_CERT_DIR must be an absolute path' ;; esac
            case "${DERP_PORT}" in *[!0-9]*|'') die 'invalid DERP port' ;; esac
            [ "${DERP_PORT}" -ge 1 ] && [ "${DERP_PORT}" -le 65535 ] || die 'invalid DERP port' ;;
        node) require_var SERVER_URL "${SERVER_URL:-}" ;;
    esac
}

package_urls() {
    HEADSCALE_FILE="headscale_${HEADSCALE_VERSION}_linux_${ARCH}.deb"
    HEADSCALE_URL="https://github.com/juanfont/headscale/releases/download/v${HEADSCALE_VERSION}"
    TAILSCALE_FILE="tailscale_${TAILSCALE_VERSION}_${ARCH}.tgz"
    TAILSCALE_URL="https://pkgs.tailscale.com/stable/${TAILSCALE_FILE}"
    GO_FILE="go${GO_VERSION}.linux-${ARCH}.tar.gz"
}

print_plan() {
    printf 'Installer: %s\n' "${VERSION}" >&2
    case "${ROLE}" in
        control)
            for file in config.yaml derp.yaml policy.hujson; do
                printf '%s -> %s/etc/headscale/%s (compare; --force required if different)\n' \
                    "${INSTALL_BASE_URL%/}/files/${file}" "${TB_PREFIX}" "${file}" >&2
            done
            printf '%s/%s -> official headscale service\n' "${HEADSCALE_URL}" "${HEADSCALE_FILE}" >&2 ;;
        node)
            printf '%s -> official tailscaled service; register after daemon startup\n' "${TAILSCALE_URL}" >&2
            if [ "${ADVERTISE_EXIT_NODE:-false}" = true ]; then
                printf 'Enable IPv4/IPv6 forwarding via %s/etc/sysctl.d/99-tailbridge-exit-node.conf; advertise exit after registration\n' "${TB_PREFIX}" >&2
            fi ;;
        derp)
            printf 'https://go.dev/dl/%s; tailscale.com/cmd/derper@v%s -> /usr/local/bin/derper\n' "${GO_FILE}" "${TAILSCALE_VERSION}" >&2
            printf '%s/files/derper.service -> %s/etc/systemd/system/derper.service; enable derper\n' "${INSTALL_BASE_URL%/}" "${TB_PREFIX}" >&2
            printf '%s/files/derper.conf -> %s/etc/derper/derper.conf (compare; --force required if different)\n' "${INSTALL_BASE_URL%/}" "${TB_PREFIX}" >&2
            printf 'TLS: %s/%s.crt and .key; TCP %s, UDP 3478; identity: /var/lib/derper/derper.key\n' \
                "${DERP_CERT_DIR}" "${DERP_HOSTNAME}" "${DERP_PORT}" >&2 ;;
    esac
}

ensure_root() { [ "${TB_OFFLINE}" = 1 ] || [ "$(id -u)" -eq 0 ] || missing 'run as root'; }
cleanup() {
    if [ "${CONFIG_PENDING}" = 1 ]; then
        # Restore the complete configuration set before removing temporary backups
        restore_failed=0
        for file in ${CONFIG_FILES}; do
            target="${TB_PREFIX}${file}"
            if [ -f "${TMP}/original${file}" ]; then
                restored=$(mktemp "${target}.XXXXXX") &&
                    cp -p "${TMP}/original${file}" "${restored}" &&
                    mv -f "${restored}" "${target}" || restore_failed=1
                [ -z "${restored}" ] || rm -f "${restored}"
            else
                rm -f "${target}" || restore_failed=1
            fi
        done
        if [ "${restore_failed}" = 1 ]; then
            warn "configuration restore failed; backups retained in ${TMP}/original"
            return 1
        fi
        warn 'configuration restored after installation failure'
    fi
    if [ -n "${POLICY_GUARD}" ]; then
        rm -f /usr/sbin/policy-rc.d
        if [ -e "${TMP}/policy-rc.d" ]; then mv "${TMP}/policy-rc.d" /usr/sbin/policy-rc.d; fi
    fi
    [ -z "${STAGED}" ] || rm -f "${STAGED}"
    [ -z "${TMP}" ] || rm -rf "${TMP}"
}
prepare_tmp() {
    umask 077
    TMP=$(mktemp -d)
    trap cleanup EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
}

backup_configuration() {
    # Callers supply fixed absolute paths; retain modes for rollback
    for file in "$@"; do
        target="${TB_PREFIX}${file}"
        if [ -f "${target}" ]; then
            mkdir -p "${TMP}/original${file%/*}"
            cp -p "${target}" "${TMP}/original${file}"
        fi
    done
    CONFIG_FILES=$*
    CONFIG_PENDING=1
}

install_tool() {
    [ "${TB_OFFLINE}" != 1 ] || return 0
    command -v "$1" >/dev/null 2>&1 && return 0
    apt-get update >&2 || die 'apt index update failed'
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${2:-$1}" ca-certificates >&2 || die 'dependency installation failed'
}

download() {
    if [ "${TB_OFFLINE}" = 1 ]; then
        require_var TB_TEMPLATE_DIR "${TB_TEMPLATE_DIR:-}"
        cp "${TB_TEMPLATE_DIR}/${1##*/}" "$2" || die 'offline template is unavailable'
        return
    fi
    curl -q --fail --silent --show-error --location --retry 4 --retry-all-errors --retry-delay 10 \
        --connect-timeout 10 --max-time 1800 --speed-limit 1024 --speed-time 60 \
        --output "$2" "$1" || die 'download failed'
}

write_replaced() {
    if [ "${ROLE}" = derp ]; then
        sed -e "s|\${SERVER_URL}|${SERVER_URL}|g" \
            -e "s|\${DERP_HOSTNAME}|${DERP_HOSTNAME}|g" \
            -e "s|\${DERP_PORT}|${DERP_PORT}|g" \
            -e "s|\${DERP_CERT_DIR}|${DERP_CERT_DIR}|g" "$1" > "$2" || die 'template replacement failed'
    else
        sed -e "s|\${SERVER_URL}|${SERVER_URL}|g" \
        -e "s|\${BASE_DOMAIN}|${BASE_DOMAIN}|g" \
        -e "s|\${ACME_EMAIL}|${ACME_EMAIL:-}|g" \
        -e "s|\${TLS_HOSTNAME}|${TLS_HOSTNAME:-}|g" \
        -e "s|\${TLS_CERT_PATH}|${TLS_CERT_PATH:-}|g" \
        -e "s|\${TLS_KEY_PATH}|${TLS_KEY_PATH:-}|g" \
        -e "s|\${DERP_REGION_ID}|${DERP_REGION_ID}|g" \
        -e "s|\${DERP_REGION_CODE}|${DERP_REGION_CODE}|g" \
        -e "s|\${DERP_REGION_NAME}|${DERP_REGION_NAME}|g" \
        -e "s|\${DERP_NODE_NAME}|${DERP_NODE_NAME}|g" \
        -e "s|\${DERP_HOSTNAME}|${DERP_HOSTNAME}|g" \
        -e "s|\${DERP_IPV4}|${DERP_IPV4:-}|g" \
        -e "s|\${DERP_PORT}|${DERP_PORT}|g" "$1" > "$2" || die 'template replacement failed'
    fi
    if grep -Fq "\${" "$2" || grep -q 'REPLACE_' "$2"; then die 'unresolved template placeholder'; fi
}
apply_template() {
    download "${INSTALL_BASE_URL%/}/files/$1" "${TMP}/$1.source"
    write_replaced "${TMP}/$1.source" "${TMP}/$1"
}

check_overwrite() {
    conflict=0
    for file in config.yaml derp.yaml policy.hujson; do
        target="${TB_PREFIX}/etc/headscale/${file}"
        [ ! -L "${target}" ] || die "refusing configuration symlink: ${target}"
        [ ! -e "${target}" ] || [ -f "${target}" ] || die "configuration is not a regular file: ${target}"
        if [ -e "${target}" ] && ! cmp -s "${TMP}/${file}" "${target}"; then
            conflict=1
            warn "configuration differs: ${target}"
            case "${file}" in
                policy.hujson) warn 'replacing policy.hujson resets access grants to deny all' ;;
                derp.yaml) warn 'replacing derp.yaml replaces the existing relay regions' ;;
            esac
        fi
    done
    [ "${conflict}" = 0 ] || [ "${FORCE}" = 1 ] || die 'configuration exists; use --force to replace it'
}

keep_state() {
    HAD_STATE=0
    if [ -e "${TB_PREFIX}/var/lib/tailscale/tailscaled.state" ]; then
        HAD_STATE=1
        if [ "${ROLE}" = node ] && [ ! -s "${TB_PREFIX}/var/lib/tailscale/tailscaled.state" ]; then
            missing 'existing node state is empty; explicit recovery is required'
        fi
    fi
}

verify_checksum() {
    checksum=$1
    printf '%s\n' "${checksum}" | LC_ALL=C grep -Eq '^[a-fA-F0-9]{64}$' || die 'missing or invalid official checksum'
    printf '%s  %s\n' "${checksum}" "$2" > "${TMP}/check.sha256"
    sha256sum --check "${TMP}/check.sha256" >&2 || die 'checksum mismatch'
}

fetch_package() {
    [ "${TB_OFFLINE}" != 1 ] || return 0
    if [ "${ROLE}" = control ]; then
        if command -v headscale >/dev/null 2>&1 && [ "$(headscale version 2>/dev/null | sed -n '1p')" = "headscale version v${HEADSCALE_VERSION}" ]; then return; fi
        download "${HEADSCALE_URL}/${HEADSCALE_FILE}" "${TMP}/${HEADSCALE_FILE}"
        download "${HEADSCALE_URL}/checksums.txt" "${TMP}/checksums.txt"
        checksum=$(awk -v file="${HEADSCALE_FILE}" '$2 == file || $2 == "*" file {print $1}' "${TMP}/checksums.txt")
        verify_checksum "${checksum}" "${TMP}/${HEADSCALE_FILE}"
        # Defer service startup during package installation; preserve policy-rc.d
        if [ -e /usr/sbin/policy-rc.d ] || [ -L /usr/sbin/policy-rc.d ]; then mv /usr/sbin/policy-rc.d "${TMP}/policy-rc.d"; fi
        POLICY_GUARD=1
        printf '#!/bin/sh\nexit 101\n' > /usr/sbin/policy-rc.d
        chmod 755 /usr/sbin/policy-rc.d
        chmod 755 "${TMP}"
        DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
            -o Dpkg::Options::=--force-confold "${TMP}/${HEADSCALE_FILE}" >&2 || die 'Headscale package installation failed'
        rm /usr/sbin/policy-rc.d
        if [ -e "${TMP}/policy-rc.d" ]; then mv "${TMP}/policy-rc.d" /usr/sbin/policy-rc.d; fi
        POLICY_GUARD=
        [ "$(headscale version | sed -n '1p')" = "headscale version v${HEADSCALE_VERSION}" ] || die 'installed Headscale version mismatch'
    else
        if [ -f "${TB_PREFIX}/lib/systemd/system/tailscaled.service" ] && \
            [ -f "${TB_PREFIX}/etc/default/tailscaled" ] && \
            command -v tailscale >/dev/null 2>&1 && command -v tailscaled >/dev/null 2>&1 && \
            [ "$(tailscale version | sed -n '1p')" = "${TAILSCALE_VERSION}" ] && \
            [ "$(tailscaled --version | sed -n '1p')" = "${TAILSCALE_VERSION}" ]; then return; fi
        download "${TAILSCALE_URL}" "${TMP}/${TAILSCALE_FILE}"
        download "${TAILSCALE_URL}.sha256" "${TMP}/tailscale.sha256"
        verify_checksum "$(cat "${TMP}/tailscale.sha256")" "${TMP}/${TAILSCALE_FILE}"
        tar -xzf "${TMP}/${TAILSCALE_FILE}" -C "${TMP}" || die 'Tailscale archive extraction failed'
        package="${TMP}/tailscale_${TAILSCALE_VERSION}_${ARCH}"
        install -d "${TB_PREFIX}/usr/bin" "${TB_PREFIX}/usr/sbin" "${TB_PREFIX}/lib/systemd/system" "${TB_PREFIX}/etc/default"
        install -m 755 "${package}/tailscale" "${TB_PREFIX}/usr/bin/tailscale"
        install -m 755 "${package}/tailscaled" "${TB_PREFIX}/usr/sbin/tailscaled"
        install -m 644 "${package}/systemd/tailscaled.service" "${TB_PREFIX}/lib/systemd/system/tailscaled.service"
        if [ ! -e "${TB_PREFIX}/etc/default/tailscaled" ]; then
            install -m 644 "${package}/systemd/tailscaled.defaults" "${TB_PREFIX}/etc/default/tailscaled"
        fi
        [ "$("${TB_PREFIX}/usr/bin/tailscale" version | sed -n '1p')" = "${TAILSCALE_VERSION}" ] || die 'installed Tailscale version mismatch'
    fi
    PACKAGE_CHANGED=1
}

fetch_go() {
    [ "${TB_OFFLINE}" != 1 ] || return 0
    binary="${TB_PREFIX}/usr/local/bin/derper"
    if [ -x "${binary}" ] && [ "$("${binary}" --version | sed -n '1{s/-ERR-BuildInfo$//;p;}')" = "${TAILSCALE_VERSION}" ]; then return; fi
    GO_BIN=$(command -v go || true)
    if [ -z "${GO_BIN}" ] || [ "$("${GO_BIN}" version)" != "go version go${GO_VERSION} linux/${ARCH}" ]; then
        download "https://go.dev/dl/${GO_FILE}" "${TMP}/${GO_FILE}"
        download 'https://go.dev/dl/?mode=json&include=all' "${TMP}/go-downloads.json"
        # Read filename and sha256 from the same official download record
        checksum=$(awk -v RS='}' -v file="${GO_FILE}" '
            { gsub(/[[:space:]]/, "") }
            index($0, "\"filename\":\"" file "\"") {
                if (match($0, /"sha256":"[a-fA-F0-9]+"/)) {
                    value=substr($0, RSTART+10, RLENGTH-11); print value
                }
            }' "${TMP}/go-downloads.json")
        verify_checksum "${checksum}" "${TMP}/${GO_FILE}"
        tar -xzf "${TMP}/${GO_FILE}" -C "${TMP}" || die 'Go archive extraction failed'
        GO_BIN="${TMP}/go/bin/go"
    fi
    [ "$("${GO_BIN}" version)" = "go version go${GO_VERSION} linux/${ARCH}" ] || die 'Go version mismatch'
    GOTOOLCHAIN=local GOBIN="${TMP}/bin" CGO_ENABLED=0 "${GO_BIN}" install \
        "tailscale.com/cmd/derper@v${TAILSCALE_VERSION}" >&2 || die 'derper compilation failed'
    install -d "${TB_PREFIX}/usr/local/bin"
    STAGED=$(mktemp "${binary}.XXXXXX")
    install -m 755 "${TMP}/bin/derper" "${STAGED}"
    mv -f "${STAGED}" "${binary}"
    STAGED=''
    PACKAGE_CHANGED=1
}

validate_control() {
    [ "${TB_OFFLINE}" != 1 ] || return 0
    install -d -o headscale -g headscale -m 750 "${TB_PREFIX}/var/lib/headscale" \
        "${TB_PREFIX}/var/lib/headscale/cache" "${TB_PREFIX}/run/headscale"
    # Grant the service ownership of existing state without changing contents or modes
    for state_file in db.sqlite db.sqlite-wal db.sqlite-shm noise_private.key; do
        state_path="${TB_PREFIX}/var/lib/headscale/${state_file}"
        if [ -e "${state_path}" ]; then chown headscale:headscale "${state_path}"; fi
    done
    if [ -n "${TLS_KEY_PATH:-}" ]; then
        chgrp headscale "${TLS_KEY_PATH}" || die 'cannot grant service access to TLS key'
        chmod 640 "${TLS_KEY_PATH}" || die 'cannot restrict TLS key permissions'
    fi
    runuser -u headscale -- headscale configtest >&2 || die 'Headscale configuration check failed'
    # Validate through the running service, or directly before the first startup
    if [ -e "${TB_PREFIX}/run/headscale/headscale.sock" ]; then
        runuser -u headscale -- headscale policy check --file /etc/headscale/policy.hujson
    else
        runuser -u headscale -- headscale policy check --file /etc/headscale/policy.hujson \
            --bypass-grpc-and-access-database-directly --force
    fi >&2 || die 'Headscale policy check failed'
}

enable_unit() {
    if [ ! -d "${TB_PREFIX}/run/systemd/system" ]; then
        warn "systemd is unavailable; caller must start ${1}"
        return
    fi
    [ "${TB_OFFLINE}" != 1 ] || return 0
    systemctl daemon-reload >&2 || die 'systemd reload failed'
    if systemctl is-active --quiet "$1" && { [ "${CHANGED}" = 1 ] || [ "${PACKAGE_CHANGED}" = 1 ]; }; then
        systemctl restart "$1" >&2 || die 'service restart failed'
    fi
    systemctl enable --now "$1" >&2 || die 'service enable failed'
}

register_node() {
    [ "${TB_OFFLINE}" != 1 ] || return 0
    deadline=$(($(date +%s) + 45))
    while :; do
        state=''
        if timeout 1 tailscale status --json > "${TMP}/status.json" 2>/dev/null; then
            state=$(sed -n 's/.*"BackendState"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "${TMP}/status.json")
            case "${state}" in Running|Stopped|NeedsLogin|NeedsMachineAuth) break ;; esac
        fi
        [ "$(date +%s)" -lt "${deadline}" ] || die 'tailscaled startup timed out'
        sleep 1
    done
    # An authenticated daemon may use a custom state path
    case "${state}" in Running|Stopped) HAD_STATE=1 ;; esac
    if [ "${HAD_STATE}" = 1 ]; then
        timeout 5 tailscale debug prefs > "${TMP}/prefs.json" 2>/dev/null ||
            missing 'cannot read existing control URL; state preserved'
        current_url=$(sed -n 's/.*"ControlURL"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "${TMP}/prefs.json")
        [ -n "${current_url}" ] && [ "${current_url%/}" = "${SERVER_URL%/}" ] ||
            missing 'existing control URL differs from SERVER_URL or is unknown; state preserved'
    fi
    [ "${state}" != Running ] || return 0
    if [ "${HAD_STATE}" = 1 ]; then
        [ "${state}" = Stopped ] || missing 'existing identity needs explicit reauthentication; state preserved'
        timeout 45 tailscale up > "${TMP}/registration.log" 2>&1 || missing 'existing identity could not resume; state preserved'
    else
        require_var NODE_AUTHKEY "${NODE_AUTHKEY:-}"
        printf '%s' "${NODE_AUTHKEY}" > "${TMP}/authkey"
        set -- --login-server="${SERVER_URL}" --auth-key="file:${TMP}/authkey" --timeout=45s
        [ -z "${NODE_HOSTNAME:-}" ] || set -- "$@" --hostname="${NODE_HOSTNAME}"
        tailscale up "$@" > "${TMP}/registration.log" 2>&1 || die 'node registration failed'
    fi
}

install_control() {
    keep_state
    for file in config.yaml derp.yaml policy.hujson; do apply_template "${file}"; done
    check_overwrite
    fetch_package
    install -d -m 755 "${TB_PREFIX}/etc/headscale"
    backup_configuration /etc/headscale/config.yaml /etc/headscale/derp.yaml /etc/headscale/policy.hujson
    for file in config.yaml derp.yaml policy.hujson; do
        target="${TB_PREFIX}/etc/headscale/${file}"
        if ! cmp -s "${TMP}/${file}" "${target}"; then
            STAGED=$(mktemp "${target}.XXXXXX")
            install -m 644 "${TMP}/${file}" "${STAGED}"
            mv -f "${STAGED}" "${target}"
            STAGED=''
            CHANGED=1
        fi
    done
    validate_control
    CONFIG_PENDING=0
    enable_unit headscale
}
install_derp() {
    apply_template derper.conf
    download "${INSTALL_BASE_URL%/}/files/derper.service" "${TMP}/derper.service"
    for file in /etc/derper/derper.conf /etc/systemd/system/derper.service; do
        target="${TB_PREFIX}${file}"
        [ ! -L "${target}" ] || die "refusing configuration symlink: ${target}"
        [ ! -e "${target}" ] || [ -f "${target}" ] || die "configuration is not a regular file: ${target}"
        if [ -e "${target}" ] && ! cmp -s "${TMP}/${file##*/}" "${target}"; then
            warn "configuration differs: ${target}"
            [ "${FORCE}" = 1 ] || die 'configuration exists; use --force to replace it'
        fi
    done
    cert="${DERP_CERT_DIR}/${DERP_HOSTNAME}.crt"
    key="${DERP_CERT_DIR}/${DERP_HOSTNAME}.key"
    [ -s "${cert}" ] && [ -s "${key}" ] || missing 'provide the DERP certificate and private key before installation'
    install_tool openssl
    openssl x509 -in "${cert}" -noout -checkend 0 > /dev/null || die 'invalid or expired DERP certificate'
    # Go rejects legacy CN-only certificates even when OpenSSL accepts them
    openssl x509 -in "${cert}" -noout -ext subjectAltName > "${TMP}/cert.san" || die 'invalid DERP certificate'
    grep -q 'DNS:' "${TMP}/cert.san" || die 'DERP certificate requires a DNS subjectAltName'
    openssl x509 -in "${cert}" -noout -checkhost "${DERP_HOSTNAME}" > /dev/null || die 'DERP certificate hostname mismatch'
    openssl x509 -in "${cert}" -noout -pubkey > "${TMP}/cert.pub" || die 'cannot read DERP certificate'
    openssl pkey -in "${key}" -passin pass: -pubout > "${TMP}/key.pub" 2>/dev/null || die 'invalid DERP private key'
    cmp -s "${TMP}/cert.pub" "${TMP}/key.pub" || die 'DERP certificate and key do not match'
    state="${TB_PREFIX}/var/lib/derper/derper.key"
    [ ! -L "${state}" ] || die 'refusing DERP identity symlink'
    [ ! -e "${state}" ] || { [ -f "${state}" ] && [ -s "${state}" ]; } || missing 'invalid DERP identity; state preserved'
    if [ "${TB_OFFLINE}" != 1 ]; then
        getent group derper >/dev/null || groupadd --system derper
        id -u derper >/dev/null 2>&1 || useradd --system --gid derper --home-dir /var/lib/derper \
            --no-create-home --shell /usr/sbin/nologin derper
        chgrp derper "${cert}" "${key}"
        chmod 640 "${cert}" "${key}"
        if ! runuser -u derper -- test -r "${cert}" || ! runuser -u derper -- test -r "${key}"; then
            die 'derper cannot read certificates; grant directory traversal and group read access'
        fi
        install -d -m 700 "${TB_PREFIX}/var/lib/derper"
        chown derper:derper "${TB_PREFIX}/var/lib/derper"
        if [ -e "${state}" ]; then chown derper:derper "${state}"; chmod 600 "${state}"; fi
    fi
    fetch_go
    if [ "${TB_OFFLINE}" != 1 ] && command -v systemd-analyze >/dev/null 2>&1; then
        systemd-analyze verify "${TMP}/derper.service" >&2 || die 'invalid derper service unit'
    fi
    install -d -m 755 "${TB_PREFIX}/etc/derper" "${TB_PREFIX}/etc/systemd/system"
    backup_configuration /etc/derper/derper.conf /etc/systemd/system/derper.service
    for file in ${CONFIG_FILES}; do
        target="${TB_PREFIX}${file}"
        if ! cmp -s "${TMP}/${file##*/}" "${target}"; then
            STAGED=$(mktemp "${target}.XXXXXX")
            install -m 644 "${TMP}/${file##*/}" "${STAGED}"
            mv -f "${STAGED}" "${target}"
            STAGED=''
            CHANGED=1
        fi
    done
    CONFIG_PENDING=0
    enable_unit derper
}
configure_forwarding() {
    [ "${ADVERTISE_EXIT_NODE:-false}" = true ] || return 0
    target="${TB_PREFIX}/etc/sysctl.d/99-tailbridge-exit-node.conf"
    printf '%s\n' 'net.ipv4.ip_forward=1' 'net.ipv6.conf.all.forwarding=1' > "${TMP}/exit-node.conf"
    [ ! -L "${target}" ] || die "refusing configuration symlink: ${target}"
    [ ! -e "${target}" ] || [ -f "${target}" ] || die "configuration is not a regular file: ${target}"
    if [ -e "${target}" ] && ! cmp -s "${TMP}/exit-node.conf" "${target}"; then
        warn "configuration differs: ${target}"
        [ "${FORCE}" = 1 ] || die 'configuration exists; use --force to replace it'
    fi
    if ! cmp -s "${TMP}/exit-node.conf" "${target}"; then
        install -d -m 755 "${TB_PREFIX}/etc/sysctl.d"
        STAGED=$(mktemp "${target}.XXXXXX")
        install -m 644 "${TMP}/exit-node.conf" "${STAGED}"
        mv -f "${STAGED}" "${target}"
        STAGED=''
    fi
    [ "${TB_OFFLINE}" != 1 ] || return 0
    install_tool sysctl procps
    # Skip writes when the host already enabled forwarding in a restricted container
    for key in net.ipv4.ip_forward net.ipv6.conf.all.forwarding; do
        current=$(sysctl -n "${key}") || die "cannot read forwarding setting: ${key}"
        if [ "${current}" != 1 ]; then
            sysctl -w "${key}=1" >&2 || die "cannot enable forwarding: ${key}"
        fi
    done
}
install_node() {
    keep_state
    if [ "${HAD_STATE}" = 0 ] && [ "${TB_OFFLINE}" != 1 ] && [ -d "${TB_PREFIX}/run/systemd/system" ]; then
        require_var NODE_AUTHKEY "${NODE_AUTHKEY:-}"
    fi
    configure_forwarding
    fetch_package
    enable_unit tailscaled
    if [ -d "${TB_PREFIX}/run/systemd/system" ]; then
        register_node
        if [ "${ADVERTISE_EXIT_NODE:-false}" = true ] && [ "${TB_OFFLINE}" != 1 ]; then
            timeout 45 tailscale set --advertise-exit-node >&2 || die 'exit advertisement failed'
        fi
    else
        warn 'node installed; registration must follow daemon startup'
        if [ "${ADVERTISE_EXIT_NODE:-false}" = true ]; then
            warn 'forwarding enabled; caller must run tailscale set --advertise-exit-node after registration'
        fi
    fi
}

main() {
    parse_args "$@"
    detect_arch
    validate_inputs
    package_urls
    if [ "${DRY_RUN}" = 1 ]; then print_plan; return; fi
    ensure_root
    prepare_tmp
    install_tool curl
    case "${ROLE}" in
        control) install_control ;;
        derp) install_derp ;;
        node) install_node ;;
    esac
}
main "$@"
