"""部署配置与场景别名；历史状态归属独立于角色和显示名称。"""

import ipaddress
import os
from pathlib import Path
from urllib.parse import urlsplit

from config import derp_nodes, dns_name, read_yaml, validate_config

ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = ROOT.parents[1]
# 与现有外部卷及凭据文件一一对应，不能随场景别名交换。
CREDENTIALS = {"client-macbook": "macbook", "client-macmini": "macmini",
               "client-server": "workstation"}


class Settings:
    def __init__(self, data, runtime=None):
        if not isinstance(data, dict) or set(data) != {
                "project", "base_domain",
                "server_url", "webui_port", "derp_map", "nodes", "aliases", "exit_probe"}:
            raise ValueError("Environment configuration has missing or unknown fields")
        self.data = data
        self.deployment = validate_config({
            key: value for key, value in data.items() if key not in ("nodes", "aliases", "exit_probe")
        })
        self.runtime = Path(runtime or PROJECT_ROOT / "local/e2e").resolve()
        self.generated = self.runtime / "generated"
        self.bundle = self.runtime / "bundle"
        self.project = self.deployment["project"]
        self.base_domain = self.deployment["base_domain"]
        self.domain = self.base_domain
        self.relay_nodes = derp_nodes(self.deployment)
        if len(self.relay_nodes) != 1:
            raise ValueError("Installer E2E supports one DERP region")
        region = next(iter(self.deployment["derp_map"]["regions"].values()))
        # Compose 服务和状态卷固定，Map 中的地域与节点名由配置决定。
        self.relays = ["derp-main"]
        self.server_url = self.deployment["server_url"]
        self.server_host = urlsplit(self.server_url).hostname
        self.webui_port = self.deployment["webui_port"]
        self.webui_url = f"http://127.0.0.1:{self.webui_port}"
        self.nodes = data["nodes"]
        if not isinstance(self.nodes, dict) or set(self.nodes) != set(CREDENTIALS):
            raise ValueError("This topology requires the three configured client services")
        if any(not isinstance(name, str) or "." in name or not name.startswith("client-")
               or not dns_name(f"{name}.{self.domain}") for name in self.nodes.values()):
            raise ValueError("Node hostnames must be client-* DNS labels")
        if len(set(self.nodes.values())) != len(self.nodes):
            raise ValueError("Node hostnames must be unique")
        self.aliases = data["aliases"]
        if (not isinstance(self.aliases, dict)
                or set(self.aliases) != {"client-a", "client-b", "target", "exit"}
                or any(not isinstance(name, str) or name not in self.nodes
                       for name in self.aliases.values())):
            raise ValueError("Every scenario alias must reference an existing service")
        if (self.aliases["target"] != "client-server"
                or self.aliases["exit"] != "client-server"
                or {self.aliases["client-a"], self.aliases["client-b"]}
                != {"client-macbook", "client-macmini"}):
            raise ValueError("Aliases must match the SSH and exit capabilities of this topology")
        self.exit_probe = data["exit_probe"]
        if not isinstance(self.exit_probe, dict) or set(self.exit_probe) != {
                prefix + f"ipv{family}_{field}" for prefix in ("", "link_")
                for family in (4, 6) for field in ("subnet", "address")}:
            raise ValueError("Exit probe requires explicit IPv4 and IPv6 subnets and addresses")
        if any(not isinstance(value, str) for value in self.exit_probe.values()):
            raise ValueError("Exit probe networks and addresses must be strings")
        # 非公网可达的实验地址，但不使用 autogroup:internet 排除的私有地址范围。
        for family, pool in ((4, "198.18.0.0/15"), (6, "2001:db8::/32")):
            networks = []
            for prefix in ("", "link_"):
                subnet = ipaddress.ip_network(self.exit_probe[f"{prefix}ipv{family}_subnet"])
                address = ipaddress.ip_address(self.exit_probe[f"{prefix}ipv{family}_address"])
                if prefix and subnet.num_addresses < (8 if family == 4 else 4):
                    raise ValueError("Exit link must accommodate three hosts: Docker gateway, HTTP host and exit")
                if (subnet.version != family or address.version != family
                        or not subnet.subnet_of(ipaddress.ip_network(pool))
                        or address not in subnet or int(address) <= int(subnet.network_address) + 1
                        or address == subnet.broadcast_address):
                    raise ValueError(f"Invalid isolated IPv{family} exit probe network or address")
                networks.append(subnet)
            if networks[0].overlaps(networks[1]):
                raise ValueError("HTTP target must be outside the exit node's directly connected network")
        self.credentials = CREDENTIALS
        self.ssh_alias = "target.tb"
        self.compose = ["docker", "compose", "-p", self.project,
                        "--project-directory", str(ROOT), "-f", str(ROOT / "compose.yaml")]
        # 迁移机器可显式保留已有外部卷；新实例不需要此文件。
        volumes = self.runtime / f"volumes.{self.project}.yaml"
        if volumes.exists():
            self.compose.extend(["-f", str(volumes)])
        self.env = {**os.environ,
                    "TB_E2E_SOURCE": str(ROOT), "TB_E2E_GENERATED": str(self.generated),
                    "TB_E2E_BUNDLE": str(self.bundle), "TB_INSTALL_SOURCE": str(PROJECT_ROOT),
                    "TB_WEBUI_PORT": str(self.webui_port), "TB_SERVER_HOST": self.server_host,
                    "SERVER_URL": self.server_url, "BASE_DOMAIN": self.base_domain,
                    "DERP_REGION_ID": str(region["regionid"]),
                    "DERP_REGION_CODE": region["regioncode"],
                    "DERP_REGION_NAME": region["regionname"],
                    "DERP_NODE_NAME": self.relay_nodes[0]["name"],
                    "DERP_HOSTNAME": self.relay_nodes[0]["hostname"],
                    "DERP_PORT": str(self.relay_nodes[0]["derpport"]),
                    "DERP_IPV4": self.relay_nodes[0].get("ipv4", "")}
        self.env.update({"TB_" + service.upper().replace("-", "_") + "_HOSTNAME": hostname
                         for service, hostname in self.nodes.items()})
        self.env.update({"TB_EXIT_PROBE_" + key.upper(): value for key, value in self.exit_probe.items()})

    def service(self, alias):
        return self.aliases[alias]

    def hostname(self, alias):
        return self.nodes[self.service(alias)]

    def fqdn(self, alias):
        return f"{self.hostname(alias)}.{self.domain}"


def load_config(path=None):
    source = Path(path or os.environ.get("TB_CONFIG") or PROJECT_ROOT / "local/e2e/environment.yaml").resolve()
    return Settings(read_yaml(source), source.parent)
