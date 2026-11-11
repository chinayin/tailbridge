#!/usr/bin/env bash
# Provide routed HTTP test addresses without a Tailscale identity
set -euo pipefail

ip address replace "${TB_EXIT_PROBE_IPV4_ADDRESS:?Required HTTP target}/32" dev lo
ip -6 address replace "${TB_EXIT_PROBE_IPV6_ADDRESS:?Required HTTP target}/128" dev lo
exec sleep infinity
