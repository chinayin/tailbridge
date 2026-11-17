#!/usr/bin/env python3
"""Render a Certbot deployment hook for the roles on one TailBridge host."""

import argparse
from pathlib import Path
import re
import shlex
import sys

import yaml

LABEL = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"


def absolute_path(value):
    if not isinstance(value, str) or not Path(value).is_absolute():
        raise ValueError("certificate paths must be absolute")
    if ".." in Path(value).parts:
        raise ValueError("certificate paths must not contain parent components")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError("certificate paths must not contain control characters")
    return value


def default_lineage(config):
    name = config["acme"]["cert_name"]
    if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", name):
        raise ValueError("acme.cert_name must be a Certbot certificate name")
    return "/etc/letsencrypt/live/" + name


def render(config, lineage):
    roles = config["roles"]
    if not isinstance(roles, list) or not roles or len(set(roles)) != len(roles) or set(roles) - {"control", "derp"}:
        raise ValueError("roles must list control and/or derp")
    targets, services = [], []
    if "control" in roles:
        control = config["control"]
        targets += [
            ("fullchain.pem", absolute_path(control["TLS_CERT_PATH"]), "headscale", "644"),
            ("privkey.pem", absolute_path(control["TLS_KEY_PATH"]), "headscale", "640"),
        ]
        services.append("headscale.service")
    if "derp" in roles:
        derp = config["derp"]
        hostname = derp["DERP_HOSTNAME"]
        if not isinstance(hostname, str) or len(hostname) > 253 or not re.fullmatch(LABEL + r"(?:\." + LABEL + r")*", hostname):
            raise ValueError("DERP_HOSTNAME must be a DNS hostname")
        directory = absolute_path(derp.get("DERP_CERT_DIR", "/etc/derper/certs"))
        targets += [
            ("fullchain.pem", str(Path(directory) / (hostname + ".crt")), "derper", "644"),
            ("privkey.pem", str(Path(directory) / (hostname + ".key")), "derper", "640"),
        ]
        services.append("derper.service")
    if len({str(Path(target[1])) for target in targets}) != len(targets):
        raise ValueError("certificate destinations must be distinct")
    guard = '[ "${RENEWED_LINEAGE:-}" = ' + shlex.quote(absolute_path(lineage)) + ' ] || exit 0\n'
    body = '''staged=''
trap '[ -z "$staged" ] || rm -f "$staged"' 0

publish() {
    staged=$(mktemp "${2}.XXXXXX")
    install -o root -g "$3" -m "$4" "${RENEWED_LINEAGE}/$1" "$staged"
    mv -f "$staged" "$2"
    staged=''
}

'''
    calls = "\n".join("publish " + " ".join(shlex.quote(arg) for arg in target) for target in targets)
    restarts = "".join(f"systemctl try-restart {service} || status=1\n" for service in services)
    return "#!/bin/sh\nset -eu\n\n" + guard + body + calls + "\n\nstatus=0\n" + restarts + 'exit "$status"\n'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("--lineage", help="Certbot lineage directory (default: from acme.cert_name)")
    args = parser.parse_args()
    try:
        config = yaml.safe_load(args.config.read_text(encoding="utf-8"))
        hook = render(config, args.lineage or default_lineage(config))
    except (OSError, KeyError, TypeError, ValueError, yaml.YAMLError):
        print("Error: invalid configuration; check roles, acme.cert_name, hostname and distinct absolute "
              "certificate paths", file=sys.stderr)
        return 1
    sys.stdout.write(hook)
    return 0


if __name__ == "__main__":
    sys.exit(main())
