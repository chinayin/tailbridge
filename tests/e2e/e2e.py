#!/usr/bin/env python3
"""Manage the local TailBridge E2E environment. Exit codes: 0 success, 1 error, 2 missing tool."""

import argparse
import json
import os
import secrets
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

from settings import load_config

ROOT = Path(__file__).resolve().parent
GENERATED = None
CONFIG = None
# 仅供模拟设备引导使用，不作为环境配置或生产注册规则。
TEST_USER = "e2e"
# 首次启动会下载官方包并编译中继；查询本身仍使用短超时。
INSTALL_STARTUP_TIMEOUT = 1800


class Parser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(1, f"Error: {message}\n")


def run(args, *, capture=True, check=True, env=None, timeout=45, input=None):
    result = subprocess.run(args, text=True, stdout=subprocess.PIPE if capture else sys.stderr,
                            stderr=subprocess.PIPE if capture else sys.stderr, env=env, timeout=timeout, input=input)
    if check and result.returncode:
        # 不输出参数，避免未来含认证参数的命令进入诊断。
        raise RuntimeError(result.stderr.strip()[-2000:] if capture else "Command failed; see output above")
    return result


def compose(*args, bootstrap=False, **kwargs):
    extra = ["-f", str(ROOT / "compose.bootstrap.yaml")] if bootstrap else []
    return run([*CONFIG.compose, *extra, *args], env=CONFIG.env, **kwargs)


def headscale(*args):
    output = compose("exec", "-T", "control", "headscale", *args, "--output", "json")
    return json.loads(output.stdout)


def private_file(name, content):
    path = GENERATED / name
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # 原地更新文件，已有只读 bind mount 仍可看到新内容。
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(content)
    path.chmod(0o600)


def prepare_certificate(host, name, cert, key):
    if cert.exists() and run(["openssl", "x509", "-in", str(cert), "-noout",
                             "-checkhost", host], check=False).returncode == 0:
        return
    key_args = ["-key", str(key)] if key.exists() else ["-newkey", "rsa:2048", "-nodes", "-keyout", str(key)]
    with tempfile.TemporaryDirectory() as folder:
        csr = Path(folder) / f"{name}.csr"
        san = Path(folder) / f"{name}.san.cnf"
        run(["openssl", "req", "-new", *key_args, "-subj", f"/CN={host}", "-out", str(csr)])
        private_file(san, f"subjectAltName=DNS:{host}\nextendedKeyUsage=serverAuth\n")
        run(["openssl", "x509", "-req", "-in", str(csr), "-CA", str(GENERATED / "ca.crt"),
             "-CAkey", str(GENERATED / "ca.key"), "-CAcreateserial", "-days", "30",
             "-extfile", str(san), "-out", str(cert)])
    key.chmod(0o600)


def prepare():
    GENERATED.mkdir(parents=True, mode=0o700, exist_ok=True)
    GENERATED.chmod(0o700)
    for directory in (CONFIG.bundle, CONFIG.bundle / "tls", CONFIG.bundle / "secrets", CONFIG.bundle / "config"):
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
    if not (GENERATED / "ca.crt").exists():
        run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "30",
             "-subj", "/CN=tailbridge-ca", "-keyout", str(GENERATED / "ca.key"),
             "-out", str(GENERATED / "ca.crt")])
    # Non-root services need the public CA through their individual bind mounts
    (GENERATED / "ca.crt").chmod(0o644)
    prepare_certificate(CONFIG.server_host, "control", CONFIG.bundle / "tls/fullchain.pem",
                        CONFIG.bundle / "secrets/control_tls_key")
    for service, node in zip(CONFIG.relays, CONFIG.relay_nodes):
        prepare_certificate(node["hostname"], service, CONFIG.bundle / f"tls/{service}.crt",
                            CONFIG.bundle / f"secrets/{service}.key")
    for name in ("macbook.sshkey", "macmini.sshkey", "workstation.hostkey"):
        if not (GENERATED / name).exists():
            run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(GENERATED / name)])
        (GENERATED / name).chmod(0o600)
    (GENERATED / "ca.key").chmod(0o600)
    (CONFIG.bundle / "secrets/control_tls_key").chmod(0o600)
    private_file("authorized_keys", "".join((GENERATED / f"{name}.sshkey.pub").read_text()
                                            for name in ("macbook", "macmini")))
    host_key = (GENERATED / "workstation.hostkey.pub").read_text().split()
    private_file("known_hosts", f"{CONFIG.ssh_alias} {host_key[0]} {host_key[1]}\n")
    for name in CONFIG.credentials.values():
        if not (GENERATED / f"{name}.authkey").exists():
            private_file(f"{name}.authkey", "pending\n")
    # 本机只准备测试凭据与可选 UI；Headscale 配置由目标容器下载并替换。
    private_file(CONFIG.bundle / 'config/headplane.yaml',
                 f'server:\n  host: 0.0.0.0\n  port: 3000\n  base_url: {CONFIG.webui_url}\n'
                 '  cookie_secret_path: /run/secrets/webui_cookie_secret\n'
                 '  cookie_secure: false\n  data_path: /var/lib/headplane\n'
                 f'headscale:\n  url: {CONFIG.server_url}\n  config_path: /etc/headscale/config.yaml\n'
                 'integration:\n  agent: {enabled: false}\n  docker: {enabled: false}\n  proc: {enabled: false}\n')


def exit_policy(config, states):
    # 根据真实地址授权，兼容保留历史注册用户的节点，不将场景别名当成身份。
    return {"grants": [
        {"src": ["*"], "dst": [ip for state in states.values() for ip in state["ips"]], "ip": ["*"]},
        {"src": states[config.service("client-a")]["ips"], "dst": ["autogroup:internet"], "ip": ["*"]},
    ]}


def configure_exit_node():
    states = {}
    for service in CONFIG.nodes:
        own = json.loads(compose("exec", "-T", service, "tailscale", "status", "--json").stdout)["Self"]
        states[service] = {"id": own["ID"], "ips": own["TailscaleIPs"]}
    # 由控制面写入并校验同一文件视图，避免宿主机共享目录的元数据延迟。
    compose('exec', '-T', 'control', 'python3', '/app/publish_policy.py',
            input=json.dumps(exit_policy(CONFIG, states), indent=2) + '\n')
    compose("exec", "-T", "control", "headscale", "policy", "check", "--file", "/etc/headscale/policy.hujson")
    compose("kill", "--signal", "SIGHUP", "control")
    headscale("nodes", "approve-routes", "--identifier", str(states[CONFIG.service("exit")]["id"]),
              "--routes", "0.0.0.0/0,::/0")


def up(bootstrap=False):
    prepare()
    compose("build", "control", capture=False, timeout=600)
    # Headscale 不热加载域名；确保新的生成配置实际被进程读取。
    compose("up", "-d", "--force-recreate", "control", capture=False, timeout=600)
    compose("up", "-d", "--no-build", "probe", "httpbin", *CONFIG.relays, capture=False, timeout=600)
    users = None
    deadline = time.monotonic() + INSTALL_STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        try:
            users = headscale("users", "list") or []
            break
        except (RuntimeError, json.JSONDecodeError):
            time.sleep(1)
    if users is None:
        raise RuntimeError("Headscale startup timed out; inspect control logs")
    if bootstrap:
        user = next((user for user in users if user["name"] == TEST_USER), None)
        if user is None:
            headscale("users", "create", TEST_USER)
            user = next(user for user in headscale("users", "list")
                        if user["name"] == TEST_USER)
        for name in CONFIG.credentials.values():
            key = headscale("preauthkeys", "create", "--user", str(user["id"]), "--expiration", "1h")
            private_file(f"{name}.authkey", key["key"] + "\n")
        try:
            compose("up", "-d", "--no-build", *CONFIG.nodes, bootstrap=True, capture=False, timeout=600)
            synchronize_names()
        finally:
            # 注册成功或失败都移除引导密钥挂载；已有身份保存在原状态卷。
            compose("up", "-d", "--no-build", *CONFIG.nodes, capture=False, timeout=600)
    else:
        compose("up", "-d", "--no-build", *CONFIG.nodes, capture=False, timeout=600)
    synchronize_names()
    verify_initial_policy()
    configure_exit_node()
    print("E2E environment started. Run verify.py to validate SSH, DERP and exit routing.", file=sys.stderr)


def synchronize_names():
    identities = {}
    for service, hostname in CONFIG.nodes.items():
        deadline = time.monotonic() + INSTALL_STARTUP_TIMEOUT
        while time.monotonic() < deadline:
            try:
                status = json.loads(compose("exec", "-T", service, "tailscale", "status", "--json").stdout)
                if status["BackendState"] == "Running" and status["Self"]["HostName"] == hostname:
                    identities[service] = str(status["Self"]["ID"])
                    break
            except (RuntimeError, KeyError, json.JSONDecodeError):
                pass
            time.sleep(1)
        else:
            raise RuntimeError(f"Node startup timed out: {service}")
    if len(set(identities.values())) != len(CONFIG.nodes):
        raise RuntimeError("Node identities are not independent; refusing to rename")
    registered = {str(node["id"]): node for node in headscale("nodes", "list")}
    # hostname 与持久化 given_name 分离；使用已登录节点 ID 更名，保留身份。
    for service, identifier in identities.items():
        if identifier not in registered:
            raise RuntimeError(f"Registered node identity not found: {service}")
        if registered[identifier]["given_name"] != CONFIG.nodes[service]:
            headscale("nodes", "rename", CONFIG.nodes[service], "--identifier", identifier)


def verify_initial_policy():
    policy = json.loads((CONFIG.bundle / 'config/policy.hujson').read_text())
    if policy != {'grants': []}:
        raise RuntimeError('Installer did not initialize a deny-all policy')
    target = json.loads(compose('exec', '-T', CONFIG.service('target'),
                               'tailscale', 'status', '--json').stdout)['Self']['TailscaleIPs'][0]
    deadline = time.monotonic() + INSTALL_STARTUP_TIMEOUT
    while True:
        ready = compose('exec', '-T', CONFIG.service('target'), 'python3', '-c',
                        'import socket,sys; socket.create_connection((sys.argv[1],22),timeout=1).close()',
                        target, check=False)
        relay = compose('exec', '-T', CONFIG.service('client-a'), 'curl', '-q', '--fail', '--silent',
                        '--max-time', '3', f'https://{CONFIG.relay_nodes[0]["hostname"]}:{CONFIG.relay_nodes[0]["derpport"]}/',
                        check=False)
        if ready.returncode == 0 and relay.returncode == 0:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError('SSH target or private DERP startup timed out')
        time.sleep(1)
    # 已有节点重连后才会收到新策略，不能把旧的允许规则当作初始策略验证。
    deadline = time.monotonic() + 45
    for service in CONFIG.nodes:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise RuntimeError('Initial deny policy propagation timed out')
            try:
                netmap = json.loads(compose('exec', '-T', service, 'tailscale', 'debug', 'netmap',
                                           timeout=min(5, remaining)).stdout)
            except (RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired):
                pass
            else:
                if netmap.get('PacketFilter') == []:
                    break
            time.sleep(min(1, max(0, deadline - time.monotonic())))
    result = compose('exec', '-T', CONFIG.service('client-a'), 'ssh', '-F', '/dev/null',
                     '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3',
                     '-o', 'StrictHostKeyChecking=yes', '-o', f'HostKeyAlias={CONFIG.ssh_alias}',
                     '-o', 'UserKnownHostsFile=/etc/tailbridge/ssh/known_hosts',
                     '-i', '/run/secrets/ssh_client_key', f'root@{target}', 'true', check=False)
    if result.returncode == 0:
        raise RuntimeError('Node bypassed the initial deny policy')
    private_file('install-results.json', json.dumps({'passed': True, 'default_policy': policy,
                 'ssh_denied_before_grant': True, 'installer': 'install.sh',
                 'templates': 'files/', 'offline': False}, indent=2) + '\n')


def ui():
    if not (CONFIG.bundle / "config/config.yaml").exists():
        raise RuntimeError("Start the E2E environment with 'up' first")
    secret_path = CONFIG.bundle / "secrets/webui_cookie_secret"
    if not secret_path.exists():
        private_file(CONFIG.bundle / "secrets/webui_cookie_secret", secrets.token_hex(16))
    key_path = GENERATED / "headplane-api-key"
    if not key_path.exists():
        key = headscale("apikeys", "create", "--expiration", "168h")
        if not isinstance(key, str) or not key.strip():
            raise RuntimeError("Unexpected Headscale API key response")
        private_file(key_path.name, key.strip() + "\n")
    compose("--profile", "ui", "up", "-d", "--force-recreate", "--no-deps", "webui", capture=False, timeout=600)
    print(f"Web UI: {CONFIG.webui_url}/admin", file=sys.stderr)
    print(f"Login API key file: {key_path}", file=sys.stderr)


def main():
    global CONFIG, GENERATED
    parser = Parser(description=__doc__)
    parser.add_argument("--version", action="version", version="0.3.0")
    parser.add_argument("--config", help="Shared environment YAML (default: local/e2e/environment.yaml or TB_CONFIG)")
    parser.add_argument("--bootstrap", action="store_true",
                        help="Issue and temporarily mount registration keys (up only)")
    for component in ('headscale', 'tailscale'):
        parser.add_argument(f'--{component}-version', help='Override the shared installer default')
    parser.add_argument("action", choices=("up", "ui", "status", "down", "compose"))
    parser.add_argument("compose_args", nargs=argparse.REMAINDER, help="Arguments for the compose action")
    args = parser.parse_args()
    if args.compose_args and args.action != "compose":
        parser.error("Extra arguments are only accepted by the compose action")
    if args.bootstrap and args.action != "up":
        parser.error("--bootstrap is only accepted with up")
    required = ("docker", "openssl", "ssh-keygen") if args.action == "up" else ("docker",)
    for tool in required:
        if not shutil.which(tool):
            print(f"Error: required tool not found: {tool}", file=sys.stderr)
            return 2
    os.umask(0o077)
    try:
        CONFIG = load_config(args.config)
        for component in ('headscale', 'tailscale'):
            version = getattr(args, f'{component}_version')
            if version:
                CONFIG.env[f'TB_{component.upper()}_VERSION'] = version
        GENERATED = CONFIG.generated
        if args.action == "up":
            up(bootstrap=args.bootstrap)
        elif args.action == "ui":
            ui()
        elif args.action == "down":
            compose("--profile", "*", "down", capture=False)
            print("E2E environment stopped; state volumes and credentials preserved.", file=sys.stderr)
        elif args.action == "compose":
            compose_args = args.compose_args
            if compose_args[:1] == ["--"]:
                compose_args = compose_args[1:]
            # 手工 Compose 与 E2E 使用完全相同的配置解析。
            return subprocess.run([*CONFIG.compose, *compose_args], env=CONFIG.env).returncode
        else:
            print(compose("ps").stdout, end="")
    except (RuntimeError, ValueError, KeyError, OSError, subprocess.TimeoutExpired) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
