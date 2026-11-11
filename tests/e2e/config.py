"""本机 E2E 的输入读取与格式校验；不生成部署文件。"""

import ipaddress
from pathlib import Path
import re
from urllib.parse import urlsplit


def dns_name(value):
    return (isinstance(value, str) and len(value) <= 253 and "." in value
            and all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part)
                    for part in value.split(".")) and "xxx" not in value.split("."))


def read_yaml(path):
    try:
        import yaml
    except ImportError as error:
        raise ModuleNotFoundError("Install tests/e2e/requirements.txt in a local virtual environment") from error

    class UniqueLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node):
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node)
            if type(key) not in (str, int) or key in result:
                raise ValueError("YAML keys must be unique strings or integers")
            result[key] = loader.construct_object(value_node)
        return result

    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    try:
        data = yaml.load(Path(path).read_text(), Loader=UniqueLoader)
    except yaml.YAMLError as error:
        raise ValueError("Invalid YAML configuration") from error
    return data


def validate_config(data):
    """本机 E2E 字段校验；允许测试域名，DERP 可通过 Docker DNS 寻址。"""
    if not isinstance(data, dict) or set(data) != {
            "project", "base_domain", "server_url", "derp_map", "webui_port"}:
        raise ValueError("Configuration has missing or unknown fields")
    project = data["project"]
    if not isinstance(project, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", project):
        raise ValueError("Invalid Compose project name")
    if not dns_name(data["base_domain"]):
        raise ValueError("base_domain must be a complete DNS domain without xxx placeholders")
    address = data["server_url"]
    if not isinstance(address, str) or address != address.strip():
        raise ValueError("server_url must be an HTTPS URL")
    url = urlsplit(address)
    if (url.scheme != "https" or not dns_name(url.hostname) or url.username or url.password
            or url.port not in (None, 443) or url.path not in ("", "/") or url.query or url.fragment
            or any(char.isspace() for char in address)):
        raise ValueError("server_url must be a plain HTTPS hostname on port 443")
    host = url.hostname
    validate_endpoint(host, data["base_domain"])
    validate_derp_map(data["derp_map"], data["base_domain"], host)
    if type(data["webui_port"]) is not int or not 1024 <= data["webui_port"] <= 65535:
        raise ValueError("webui_port must be an integer between 1024 and 65535")
    return {**data, "server_url": f"https://{host}"}


def validate_endpoint(host, base_domain):
    if not dns_name(host):
        raise ValueError("Service endpoint must be a complete DNS hostname")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("Service endpoint requires a DNS hostname, not an IP address")
    if host == base_domain or host.endswith("." + base_domain):
        raise ValueError("Service endpoint must be outside the MagicDNS base_domain")


def validate_derp_map(derp_map, base_domain, server_host):
    if not isinstance(derp_map, dict) or set(derp_map) != {"regions"}:
        raise ValueError("derp_map must contain native regions")
    regions = derp_map["regions"]
    if not isinstance(regions, dict) or not regions:
        raise ValueError("At least one DERP region is required")
    codes, names, hosts = set(), set(), {server_host}
    for region_id, region in regions.items():
        if type(region_id) is not int or not 1 <= region_id <= 65535:
            raise ValueError("DERP region IDs must be integers between 1 and 65535")
        if (not isinstance(region, dict)
                or set(region) != {"regionid", "regioncode", "regionname", "nodes"}
                or type(region["regionid"]) is not int or region["regionid"] != region_id):
            raise ValueError("DERP region fields or regionid are invalid")
        code = region["regioncode"]
        if not isinstance(code, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,31}", code) or code in codes:
            raise ValueError("DERP region codes must be unique lowercase labels")
        codes.add(code)
        if not isinstance(region["regionname"], str) or not region["regionname"].strip():
            raise ValueError("DERP regionname must be nonempty")
        label = region["regionname"]
        if (any(token in label for token in ('|', '&', '\\', '"', "'", '${'))
                or any(ord(char) < 32 or ord(char) == 127 for char in label)):
            raise ValueError("DERP regionname contains characters unsafe for installer templates")
        nodes = region["nodes"]
        if not isinstance(nodes, list) or len(nodes) != 1:
            raise ValueError("Use one node per region; same-region mesh is not configured")
        node = nodes[0]
        required = {"name", "regionid", "hostname", "derpport", "stunport", "canport80"}
        if (not isinstance(node, dict) or not required <= set(node)
                or set(node) - required - {"ipv4"}):
            raise ValueError("DERP node fields are invalid")
        name = node["name"]
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,47}", name) or name in names:
            raise ValueError("DERP node names must be unique lowercase labels")
        names.add(name)
        if type(node["regionid"]) is not int or node["regionid"] != region_id:
            raise ValueError("DERP node regionid must match its region")
        host = node["hostname"]
        validate_endpoint(host, base_domain)
        if host in hosts:
            raise ValueError("Control and DERP endpoints must have distinct hostnames")
        hosts.add(host)
        if "ipv4" in node:
            try:
                ip = ipaddress.IPv4Address(node["ipv4"])
            except (ipaddress.AddressValueError, TypeError) as error:
                raise ValueError("DERP ipv4 must be globally routable") from error
            if not isinstance(node["ipv4"], str) or not ip.is_global or ip.is_multicast:
                raise ValueError("DERP ipv4 must be globally routable")
        for field in ("derpport", "stunport"):
            if type(node[field]) is not int or not 1 <= node[field] <= 65535:
                raise ValueError(f"DERP {field} must be an integer between 1 and 65535")
        if node["canport80"] is not False:
            raise ValueError("canport80 must be false: this deployment disables the HTTP listener")


def derp_nodes(config):
    return [region["nodes"][0] for region in config["derp_map"]["regions"].values()]
