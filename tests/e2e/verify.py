#!/usr/bin/env python3
"""验证隔离 Docker 尾网的 SSH、DERP 和整机出口；不代表真实公网或 Mac 验收。"""

import argparse
from contextlib import contextmanager
import ipaddress
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from settings import load_config
from report import write_report

ROOT = Path(__file__).resolve().parent
CONFIG = None
NODES = ()
CLIENTS = ()
TARGET = None
REPORT = {"scope": "local Docker simulation; SSH and dual-stack exit HTTP, no public Internet", "checks": []}


def configure(path=None):
    global CONFIG, NODES, CLIENTS, TARGET
    CONFIG = load_config(path)
    NODES = tuple(CONFIG.nodes)
    CLIENTS = tuple(CONFIG.service(role) for role in ("client-a", "client-b"))
    TARGET = CONFIG.service("target")


def command(args, timeout=45, check=True, env=None):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout, env=env)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip()[-2000:] or f"Command failed: {args[0]}")
    return result


def compose(*args, **kwargs):
    return command([*CONFIG.compose, *args], env=CONFIG.env, **kwargs)


def node(service, *args, **kwargs):
    return compose("exec", "-T", service, *args, **kwargs)


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def passed(name, **details):
    REPORT["checks"].append({"name": name, "passed": True, **details})
    print(f"[PASS] {name}", file=sys.stderr, flush=True)


def eventually(operation, seconds=60):
    deadline = time.monotonic() + seconds
    last = None
    while time.monotonic() < deadline:
        try:
            return operation()
        except (RuntimeError, AssertionError, subprocess.TimeoutExpired) as error:
            last = error
            time.sleep(1)
    raise RuntimeError(f"Timed out waiting for expected network state: {last}")


def identity(service):
    status = json.loads(node(service, "tailscale", "status", "--json").stdout)
    require(status["BackendState"] == "Running", f"{service} is not Running")
    own = status["Self"]
    return {"id": own["ID"], "public_key": own["PublicKey"], "ips": own["TailscaleIPs"]}


def ubuntu_runtime():
    runtimes = {}
    for service in ("control", *NODES):
        release = compose("exec", "-T", service, "cat", "/etc/os-release").stdout
        values = dict(line.split("=", 1) for line in release.splitlines() if "=" in line)
        require(values.get("ID", "").strip('"') == "ubuntu"
                and values.get("VERSION_ID", "").strip('"') == "26.04", f"Wrong Ubuntu runtime: {service}")
        pid1 = compose("exec", "-T", service, "cat", "/proc/1/comm").stdout.strip()
        require(pid1 not in ("systemd", "init"), f"Full init system running in {service}")
        runtimes[service] = {"ubuntu": "26.04", "pid1": pid1}
    package = compose("exec", "-T", "control", "dpkg-query", "-W", "-f=${Version}", "headscale").stdout
    require(package == REPORT["versions"]["headscale"].splitlines()[0].removeprefix("headscale version v"),
            "Headscale binary does not match installed DEB")
    REPORT["versions"]["runtime"] = runtimes
    passed("Ubuntu 26.04 control and nodes run official programs without systemd", headscale_deb=package)


def ssh(service, target, check=True):
    return node(service, "ssh", "-F", "/dev/null", "-i", "/run/secrets/ssh_client_key",
                "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                "-o", "ConnectionAttempts=1", "-o", "StrictHostKeyChecking=yes",
                "-o", "UserKnownHostsFile=/etc/tailbridge/ssh/known_hosts",
                "-o", f"HostKeyAlias={CONFIG.ssh_alias}", f"root@{target}",
                "hostname; printf '%s\\n' \"$SSH_CONNECTION\"", check=check, timeout=15)


def assert_ssh(service, target, expected_client_ip):
    output = ssh(service, target).stdout.splitlines()
    require(len(output) == 2 and output[0] == CONFIG.hostname("target"),
            f"Wrong SSH destination: {output}")
    require(output[1].split()[0] == expected_client_ip, f"SSH bypassed tailnet: {output[1]}")
    return output[1]


def ping(service, target):
    return node(service, "tailscale", "ping", "--c=1", "--until-direct=false",
                "--timeout=3s", target, timeout=10).stdout.strip()


def basic():
    states = {name: eventually(lambda name=name: identity(name)) for name in NODES}
    require(len({state["id"] for state in states.values()}) == 3, "Node IDs are not independent")
    require(len({state["public_key"] for state in states.values()}) == 3, "Node keys are not independent")
    passed("three independent registered nodes", nodes=states)
    for name in NODES:
        prefs = json.loads(node(name, "tailscale", "debug", "prefs").stdout)
        require(prefs.get("ControlURL", "").rstrip("/") == CONFIG.server_url.rstrip("/"),
                f"Configured control URL is not applied: {name}")
        own = json.loads(node(name, "tailscale", "status", "--json").stdout)["Self"]
        require(own["DNSName"].rstrip(".") == f"{CONFIG.nodes[name]}.{CONFIG.domain}",
                f"Configured MagicDNS name is not applied: {name}")
        require(not prefs.get("ExitNodeID") and not prefs.get("ExitNodeIP"), f"Unexpected exit: {name}")
        expected_routes = {"0.0.0.0/0", "::/0"} if name == CONFIG.service("exit") else set()
        require(set(prefs.get("AdvertiseRoutes") or []) == expected_routes,
                f"Unexpected advertised routes: {name}")
        forwarding_file = "/etc/sysctl.d/99-tailbridge-exit-node.conf"
        if name == CONFIG.service("exit"):
            forwarding = node(name, "cat", forwarding_file).stdout
            require(forwarding == "net.ipv4.ip_forward=1\nnet.ipv6.conf.all.forwarding=1\n",
                    "Installer did not persist exit forwarding settings")
            values = node(name, "sysctl", "-n", "net.ipv4.ip_forward", "net.ipv6.conf.all.forwarding").stdout
            require(values.splitlines() == ["1", "1"], "Exit forwarding is disabled")
        else:
            require(node(name, "test", "-e", forwarding_file, check=False).returncode == 1,
                    f"Ordinary node received exit forwarding configuration: {name}")
        derp = json.loads(node(name, "tailscale", "debug", "derp-map").stdout)
        expected = CONFIG.deployment["derp_map"]["regions"]
        require(set(derp["Regions"]) == {str(key) for key in expected}, f"Unexpected DERP map: {name}")
        for key, region in expected.items():
            actual = derp["Regions"][str(key)]
            require(actual["RegionID"] == key, "Wrong relay region ID")
            require(actual["RegionCode"] == region["regioncode"], "Wrong relay region")
            require(actual["RegionName"] == region["regionname"], "Wrong relay region name")
            require(len(actual["Nodes"]) == 1, "Unexpected relay nodes")
            relay = actual["Nodes"][0]
            require(relay["Name"] == region["nodes"][0]["name"], "Wrong relay node name")
            require(relay["RegionID"] == key, "Wrong relay node region ID")
            require(relay["HostName"] == region["nodes"][0]["hostname"], "Wrong relay host")
    passed("installer persists exit forwarding; only designated exit advertises; clients opt out; private DERP map")

    destination = states[TARGET]["ips"][0]
    # 出口连接两个网络，必须明确取 service 网络，不能拼接多个接口地址。
    underlay = container_network(TARGET, "service")["IPAddress"]
    require(underlay and underlay != destination, "Invalid Docker underlay address")
    for name in CLIENTS:
        source = states[name]["ips"][0]
        path = eventually(lambda: direct_ping(name, destination))
        connection = eventually(lambda: assert_ssh(name, destination, source))
        passed(f"{name}: direct tailnet SSH", path=path, connection=connection)
        eventually(lambda: assert_ssh(name, CONFIG.fqdn("target"), source))
        passed(f"{name}: MagicDNS SSH")
        eventually(lambda: assert_ssh(name, CONFIG.hostname("target"), source))
        passed(f"{name}: short-name MagicDNS SSH")
        require(ssh(name, underlay, check=False).returncode != 0, "SSH reachable through Docker underlay")
        passed(f"{name}: SSH is unreachable through Docker underlay")
        probe = node(name, "curl", "--noproxy", "*", "-fsS", "--max-time", "5",
                     "http://probe:8080").stdout.strip()
        route = json.loads(node(name, "ip", "-json", "route", "get", underlay).stdout)[0]
        require(probe == route["prefsrc"], f"Ordinary traffic source changed: {probe}")
        passed(f"{name}: ordinary underlay path preserved", source=probe)
    return states


def container_network(service, network):
    container = compose("ps", "-q", service).stdout.strip()
    info = json.loads(command(["docker", "inspect", container]).stdout)[0]
    return info["NetworkSettings"]["Networks"][f"{CONFIG.project}_{network}"]


@contextmanager
def selected_exit(service, destination, *, check=True):
    original = json.loads(node(service, "tailscale", "debug", "prefs").stdout)
    require(not original.get("ExitNodeID") and not original.get("ExitNodeIP"),
            f"Exit already selected on {service}; disable it before running E2E")
    try:
        result = node(service, "tailscale", "set", f"--exit-node={destination}",
                      "--exit-node-allow-lan-access=false", check=check)
        yield result
    finally:
        # 选择命令超时也可能已生效，必须无条件撤销并恢复原 LAN 偏好。
        lan = str(bool(original.get("ExitNodeAllowLANAccess"))).lower()
        node(service, "tailscale", "set", "--exit-node=", f"--exit-node-allow-lan-access={lan}")
        restored = json.loads(node(service, "tailscale", "debug", "prefs").stdout)
        require(not restored.get("ExitNodeID") and not restored.get("ExitNodeIP")
                and bool(restored.get("ExitNodeAllowLANAccess")) == bool(original.get("ExitNodeAllowLANAccess")),
                f"Exit preferences were not restored: {service}")


def exit_http(service, family, *, check=True):
    address = CONFIG.exit_probe[f"ipv{family}_address"]
    host = f"[{address}]" if family == 6 else address
    # -q 禁用 curlrc；禁用代理且不发送来源头，/ip 读取真实 TCP 对端地址。
    return node(service, "curl", "-q", "--noproxy", "*", "--proxy", "", "-fsS",
                "--connect-timeout", "2", "--max-time", "4", f"http://{host}:8080/ip",
                check=check, timeout=10)


def assert_exit_origin(service, family, expected):
    response = json.loads(exit_http(service, family).stdout)
    origin = response.get("origin")
    require(origin and ipaddress.ip_address(origin) == ipaddress.ip_address(expected),
            f"Wrong IPv{family} exit source: {origin}, expected {expected}")
    route = json.loads(node(service, "ip", f"-{family}", "-json", "route", "get",
                            CONFIG.exit_probe[f"ipv{family}_address"]).stdout)[0]
    require(route.get("dev") == "tailscale0", f"Exit probe bypasses tailnet: {route}")
    return {"origin": origin, "route": route}


def assert_exit_unreachable(service, family):
    result = exit_http(service, family, check=False)
    # 排除 HTTP 错误、Docker exec 失败等假反例；只接受连接拒绝/无路由或超时。
    require(result.returncode in (7, 28),
            f"Expected IPv{family} network failure on {service}, got {result.returncode}: {result.stdout}")


def exit_routing(states, *, faults):
    gateway = CONFIG.service("exit")
    allowed, denied = CLIENTS
    destination = states[gateway]["ips"][0]
    gateway_network = container_network(gateway, "egress")
    expected = {4: gateway_network["IPAddress"], 6: gateway_network["GlobalIPv6Address"]}
    require(all(expected.values()), "Exit node has no dual-stack egress addresses")
    target_network = container_network("httpbin-network", "egress")
    require(target_network["IPAddress"] == CONFIG.exit_probe["link_ipv4_address"]
            and target_network["GlobalIPv6Address"] == CONFIG.exit_probe["link_ipv6_address"],
            "HTTP probe addresses do not match the environment")
    for service in NODES:
        prefs = json.loads(node(service, "tailscale", "debug", "prefs").stdout)
        require(not prefs.get("ExitNodeID") and not prefs.get("ExitNodeIP"), f"Exit already selected: {service}")
        routes = {"0.0.0.0/0", "::/0"} if service == gateway else set()
        require(set(prefs.get("AdvertiseRoutes") or []) == routes,
                f"Only exit default routes may be advertised: {service}")
        require(not prefs.get("RouteAll"), f"Subnet route acceptance enabled: {service}")
    for service in CLIENTS:
        info = json.loads(command(["docker", "inspect", compose("ps", "-q", service).stdout.strip()]).stdout)[0]
        require(set(info["NetworkSettings"]["Networks"]) == {f"{CONFIG.project}_service"},
                f"Client has an unexpected underlay network: {service}")
        for family in (4, 6):
            assert_exit_unreachable(service, family)
    for family in (4, 6):
        origin = eventually(lambda: json.loads(exit_http(gateway, family).stdout)["origin"])
        require(ipaddress.ip_address(origin) == ipaddress.ip_address(expected[family]), "Wrong gateway source")
    passed("isolated HTTP target is reachable only from the exit side", exit_addresses=expected)

    with selected_exit(allowed, destination):
        path = eventually(lambda: direct_ping(allowed, destination))
        evidence = {str(family): eventually(lambda family=family:
                    assert_exit_origin(allowed, family, expected[family])) for family in (4, 6)}
        for family in (4, 6):
            routes = json.loads(node(allowed, "ip", f"-{family}", "-json", "route", "show", "table", "52").stdout)
            require(any(route.get("dst") == "default" and route.get("dev") == "tailscale0" for route in routes),
                    f"IPv{family} whole-device default route is missing")
        passed("selected exit carries IPv4 and IPv6 HTTP with the exit egress source", path=path, evidence=evidence)
        eventually(lambda: assert_ssh(allowed, CONFIG.fqdn("target"), states[allowed]["ips"][0]))
        passed("MagicDNS and tailnet SSH remain usable while the exit is selected")
        if faults:
            try:
                compose("stop", gateway, timeout=30)
                for family in (4, 6):
                    assert_exit_unreachable(allowed, family)
                prefs = json.loads(node(allowed, "tailscale", "debug", "prefs").stdout)
                require(prefs.get("ExitNodeID"), "Exit selection disappeared during gateway outage")
                passed("HTTP fails while the selected exit is stopped")
            finally:
                compose("start", gateway, timeout=30)
            current = eventually(lambda: identity(gateway), seconds=90)
            require(current == states[gateway], "Exit identity changed on recovery")
            for family in (4, 6):
                eventually(lambda family=family: assert_exit_origin(allowed, family, expected[family]))
            passed("dual-stack exit HTTP recovers with the same gateway identity")
    for family in (4, 6):
        assert_exit_unreachable(allowed, family)
    passed("deselecting the exit removes access to the isolated HTTP target")

    with selected_exit(denied, destination, check=False) as selection:
        if selection.returncode:
            require("exit node" in selection.stderr.lower() or "exit-node" in selection.stderr.lower(),
                    f"Unexpected exit selection error: {selection.stderr}")
        for family in (4, 6):
            assert_exit_unreachable(denied, family)
        passed("client without an internet grant cannot use the exit", selection_exit_code=selection.returncode)
    passed("exit selections and LAN preferences restored after HTTP tests")


def direct_ping(service, destination):
    output = ping(service, destination)
    require(" via " in output and "DERP(" not in output, f"Not a direct path: {output}")
    return output


def relay_ping(service, destination):
    output = ping(service, destination)
    require(any(f"DERP({region['regioncode']})" in output
                for region in CONFIG.deployment["derp_map"]["regions"].values()), f"Not the private DERP path: {output}")
    return output


def firewall(service, action):
    # 只在本实验客户端的物理接口阻断外层 UDP；保留 Docker DNS。
    return node(service, "iptables", action, "OUTPUT", "-o", "eth0", "-p", "udp",
                "!", "--dport", "53", "-m", "comment", "--comment", "tailbridge-e2e-relay-test",
                "-j", "DROP", check=action == "-I")


def restore_faults(blocked):
    errors = []

    def attempt(label, operation):
        try:
            operation()
        except Exception as error:
            errors.append(f"{label}: {error}")

    # 各项恢复互不阻断；start 即使在 stop 超时后也会执行。
    for relay in CONFIG.relays:
        attempt(f"start {relay}", lambda relay=relay: compose("start", relay, timeout=30))
    attempt("verify services", lambda: require(
        set(CONFIG.relays) | {"control"} <= set(compose("ps", "--status", "running", "--services").stdout.split()),
        "Control or DERP is not running"))
    for name in blocked:
        def remove_rule(service=name):
            present = firewall(service, "-C").returncode
            require(present in (0, 1), f"Cannot inspect injected firewall rule: {service}")
            if present == 0:
                removed = firewall(service, "-D")
                require(removed.returncode == 0, f"Cannot remove injected firewall rule: {service}")
            require(firewall(service, "-C").returncode == 1,
                    f"Injected firewall rule remains: {service}")

        attempt(f"restore {name} firewall", remove_rule)
        attempt(f"rebind {name}", lambda name=name: node(name, "tailscale", "debug", "rebind"))
    if errors:
        raise RuntimeError("Fault cleanup failed: " + "; ".join(errors))


def relayed(states):
    blocked = []
    destination = states[TARGET]["ips"][0]
    try:
        for name in CLIENTS:
            blocked.append(name)
            firewall(name, "-I")
            node(name, "tailscale", "debug", "rebind")
        for name in CLIENTS:
            path = eventually(lambda: relay_ping(name, destination), seconds=90)
            connection = eventually(lambda: assert_ssh(name, destination, states[name]["ips"][0]))
            passed(f"{name}: SSH survives blocked peer UDP via private DERP", path=path, connection=connection)

        egress = container_network(CONFIG.service("exit"), "egress")
        with selected_exit(CLIENTS[0], states[CONFIG.service("exit")]["ips"][0]):
            evidence = {}
            for family, key in ((4, "IPAddress"), (6, "GlobalIPv6Address")):
                evidence[str(family)] = eventually(lambda family=family, key=key:
                    assert_exit_origin(CLIENTS[0], family, egress[key]))
            passed("dual-stack exit HTTP also traverses the private DERP", evidence=evidence)

        if len(CONFIG.relays) > 1:
            # 每次停一个真实在用中继，要求连接经其余区域恢复。
            active_code = json.loads(node(TARGET, "tailscale", "status", "--json").stdout)["Self"]["Relay"]
            active_region = next(region for region in CONFIG.deployment["derp_map"]["regions"].values()
                                 if region["regioncode"] == active_code)
            active = "derp-" + active_region["nodes"][0]["name"]
            compose("stop", active, timeout=30)
            for name in CLIENTS:
                path = eventually(lambda: relay_ping(name, destination), seconds=120)
                require(f"DERP({active_code})" not in path, "Stopped relay is still used")
                eventually(lambda: assert_ssh(name, destination, states[name]["ips"][0]), seconds=120)
            passed("SSH fails over to another DERP region", stopped=active)
            compose("start", active, timeout=30)

        compose("stop", *CONFIG.relays, timeout=30)
        require("control" in compose("ps", "--status", "running", "--services").stdout.split(),
                "Control stopped together with DERP")
        for name in CLIENTS:
            require(ssh(name, destination, check=False).returncode != 0,
                    "SSH unexpectedly succeeded with peer UDP and all relays unavailable")
        passed("SSH fails with all DERPs stopped while control stays running")
        compose("start", *CONFIG.relays, timeout=30)
        for name in CLIENTS:
            eventually(lambda: assert_ssh(name, destination, states[name]["ips"][0]), seconds=90)
        passed("SSH recovers after private DERP restart")
    finally:
        restore_faults(blocked)
    passed("fault injection removed; control and relays are running")


def control_reload(states):
    path = "/etc/headscale/derp.yaml"
    original = node("control", "cat", path).stdout
    baseline = json.loads(node(NODES[0], "tailscale", "debug", "derp-map").stdout)
    updated = yaml.safe_load(original)
    region_id = next(iter(updated["regions"]))
    new_name = updated["regions"][region_id]["regionname"] + " reload-test"
    updated["regions"][region_id]["regionname"] = new_name
    expected = json.loads(json.dumps(baseline))
    expected["Regions"][str(region_id)]["RegionName"] = new_name

    # 从容器内原子替换，保留权限，避免定时读取撞上半写文件。
    write_map = """
import os, pathlib, sys, tempfile
path = pathlib.Path(sys.argv[1])
state = path.stat()
fd, pending = tempfile.mkstemp(prefix='.derp.', dir=path.parent)
try:
    with os.fdopen(fd, 'w') as stream:
        stream.write(sys.argv[2])
        os.fchmod(stream.fileno(), state.st_mode & 0o777)
        os.fchown(stream.fileno(), state.st_uid, state.st_gid)
    os.replace(pending, path)
finally:
    pathlib.Path(pending).unlink(missing_ok=True)
"""

    def publish(content):
        node("control", "python3", "-c", write_map, path, content)

    def received(value):
        for name in NODES:
            actual = json.loads(node(name, "tailscale", "debug", "derp-map").stdout)
            require(actual == value, f"DERP map has not propagated to {name}")

    container = compose("ps", "-q", "control").stdout.strip()

    def started_at():
        return command(["docker", "inspect", "-f", "{{.State.StartedAt}}", container]).stdout

    def config_hashes():
        return node("control", "sha256sum", "/etc/headscale/config.yaml", path,
                    "/etc/headscale/policy.hujson", "/var/lib/headscale/noise_private.key").stdout

    started = started_at()
    try:
        publish(yaml.safe_dump(updated, sort_keys=False))
        began = time.monotonic()
        eventually(lambda: received(expected), seconds=90)
        require(started_at() == started, "Control restarted during timed DERP reload")
        passed("DERP map reaches all nodes without control restart", seconds=round(time.monotonic() - began, 1))
        hashes = config_hashes()
        compose("restart", "control", timeout=60)
        eventually(lambda: node("control", "headscale", "nodes", "list", "--output", "json"))
        require(started_at() != started, "Control did not restart")
        require(config_hashes() == hashes, "Control restart overwrote configuration or identity")
        eventually(lambda: received(expected))
        for name in NODES:
            require(identity(name) == states[name], f"Node identity changed on control restart: {name}")
        for name in CLIENTS:
            eventually(lambda: assert_ssh(name, CONFIG.fqdn("target"), states[name]["ips"][0]))
        passed("control restart preserves custom DERP map, policy, keys and node identities; SSH recovers")
    finally:
        failure = sys.exc_info()[1]
        try:
            try:
                publish(original)
            except (RuntimeError, subprocess.TimeoutExpired):
                # 控制容器无法启动时也必须恢复其宿主机绑定文件。
                command([sys.executable, "-c", write_map, str(CONFIG.bundle / "config/derp.yaml"), original])
            compose("start", "control", timeout=60)
            eventually(lambda: received(baseline), seconds=90)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            if failure:
                raise RuntimeError(f"{failure}; DERP map cleanup failed: {error}") from error
            raise
    passed("original DERP map restored and propagated without another restart")


def restart(states):
    # 模拟旧节点持久化的显式名称；恢复时不能遗漏旧设置或重新注册。
    for name in NODES:
        node(name, "tailscale", "set", f"--hostname={CONFIG.nodes[name]}")
    try:
        node(CLIENTS[0], "tailscale", "down")
        compose("restart", *NODES, timeout=60)
        for name in NODES:
            current = eventually(lambda name=name: identity(name), seconds=90)
            require(current == states[name], f"Identity changed on restart: {name}")
            prefs = json.loads(node(name, "tailscale", "debug", "prefs").stdout)
            require(prefs["Hostname"] == CONFIG.nodes[name], f"Saved hostname changed on restart: {name}")
    finally:
        compose("start", *NODES, timeout=60)
        backend = node(CLIENTS[0], "python3", "/app/node_status.py", timeout=50).stdout.strip()
        if backend == "Stopped":
            node(CLIENTS[0], "timeout", "45", "tailscale", "up", timeout=50)
        else:
            require(backend == "Running", f"Node did not recover after restart: {backend}")
    for name in CLIENTS:
        eventually(lambda: assert_ssh(name, CONFIG.fqdn("target"), states[name]["ips"][0]))
    passed("node identities, saved hostnames and MagicDNS SSH survive restart from Running and Stopped")


def runtime_layout():
    for service in NODES:
        info = json.loads(command(["docker", "inspect", compose("ps", "-q", service).stdout.strip()]).stdout)[0]
        require(info["Config"]["WorkingDir"] == "/app", f"Unexpected workdir: {service}")
        mounts = {item["Destination"] for item in info["Mounts"]}
        require("/run/secrets/tailscale_authkey" not in mounts, f"Bootstrap key is still mounted: {service}")
        require(node(service, "test", "!", "-e", "/run/secrets/tailscale_authkey", check=False).returncode == 0,
                f"Bootstrap key is still present: {service}")
        require("/var/lib/tailscale" in mounts, f"Missing persisted identity: {service}")
    passed("nodes use /app and persisted identity without bootstrap auth key mounts")


def without_ui(states):
    running = compose("--profile", "ui", "ps", "--status", "running", "--services").stdout.split()
    was_running = "webui" in running
    try:
        if was_running:
            compose("--profile", "ui", "stop", "webui", timeout=30)
        require("webui" not in compose("--profile", "ui", "ps", "--status", "running",
                                       "--services").stdout.split(), "Web UI is still running")
        for name in CLIENTS:
            eventually(lambda: assert_ssh(name, CONFIG.fqdn("target"), states[name]["ips"][0]))
    finally:
        if was_running:
            compose("--profile", "ui", "start", "webui", timeout=30)
            require("webui" in compose("--profile", "ui", "ps", "--status", "running",
                                       "--services").stdout.split(), "Web UI was not restored")
    passed("core SSH works without Web UI; original UI running state restored", ui_was_running=was_running)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--basic", action="store_true", help="Skip fault injection and restart checks")
    mode.add_argument("--exit-only", action="store_true", help="Check exit routing, permissions and gateway recovery")
    parser.add_argument("--config", help="Shared environment YAML (default: local/e2e/environment.yaml or TB_CONFIG)")
    args = parser.parse_args()
    REPORT["started_at"] = datetime.now(timezone.utc).isoformat()
    REPORT["mode"] = "exit" if args.exit_only else ("basic" if args.basic else "full")
    try:
        configure(args.config)
        REPORT["configuration"] = CONFIG.data
        require((ROOT / "compose.yaml").exists(), "E2E compose file does not exist")
        running = set(compose("ps", "--status", "running", "--services").stdout.split())
        require(set(NODES) | set(CONFIG.relays) | {"control", "probe", "httpbin", "httpbin-network"} <= running,
                "E2E stack is not running")
        REPORT["versions"] = {
            "tailscale": node(CLIENTS[0], "tailscale", "version").stdout.splitlines()[0],
            "headscale": compose("exec", "-T", "control", "headscale", "version").stdout.strip(),
        }
        ubuntu_runtime()
        states = {name: eventually(lambda name=name: identity(name)) for name in NODES} if args.exit_only else basic()
        runtime_layout()
        exit_routing(states, faults=not args.basic)
        if not args.basic and not args.exit_only:
            control_reload(states)
            relayed(states)
            restart(states)
            without_ui(states)
        REPORT["passed"] = True
    except (AssertionError, RuntimeError, ValueError, OSError, subprocess.TimeoutExpired) as error:
        REPORT["passed"] = False
        REPORT["error"] = str(error)
        print(f"[FAIL] {error}", file=sys.stderr)
    REPORT["finished_at"] = datetime.now(timezone.utc).isoformat()
    generated = CONFIG.generated if CONFIG else ROOT.parents[1] / "local/e2e/generated"
    generated.mkdir(parents=True, mode=0o700, exist_ok=True)
    output = generated / ("exit-results.json" if args.exit_only else ("basic-results.json" if args.basic else "results.json"))
    page = write_report(REPORT, output)
    print(str(output))
    print(str(page))
    return 0 if REPORT["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
