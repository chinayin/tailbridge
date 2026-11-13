#!/usr/bin/env python3
"""Test control and node with real systemd units. Exit codes: 0 pass, 1 fail."""

import json
import subprocess
import sys
import uuid

from e2e import Parser, run
from settings import load_config, PROJECT_ROOT, ROOT
from systemd_test import SystemdTest, passed, require

HOSTNAME = "control.systemd.test"
SERVER_URL = f"https://{HOSTNAME}"
CERT = "/etc/headscale/certs/control.crt"
KEY = "/etc/headscale/certs/control.key"
NODE_NAME = "systemd-node"


class ControlNodeTest(SystemdTest):
    def __init__(self, container, proxy=""):
        super().__init__(container)
        self.proxy = proxy

    def prepare(self):
        self.boot()
        self.exec("bash", "-c", "! command -v headscale && ! command -v tailscaled")
        self.exec("install", "-d", "-m", "755", "/etc/headscale/certs")
        self.exec("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                  "-subj", f"/CN={HOSTNAME}", "-addext", f"subjectAltName=DNS:{HOSTNAME}",
                  "-addext", "basicConstraints=critical,CA:TRUE",
                  "-addext", "extendedKeyUsage=serverAuth", "-keyout", KEY, "-out", CERT)
        self.exec("install", "-m", "644", CERT, "/usr/local/share/ca-certificates/tailbridge-test.crt")
        self.exec("update-ca-certificates")
        self.templates()

    def templates(self):
        run(["docker", "exec", "-d", self.container, "python3", "-m", "http.server", "8080",
             "--bind", "127.0.0.1", "--directory", "/source"])
        self.wait(lambda: self.exec("curl", "-q", "-fsS", "--noproxy", "*",
                                   "http://127.0.0.1:8080/files/config.yaml", check=False).returncode == 0,
                  "template server")

    def install(self, role, *, authkey=None, check=True, **settings):
        environment = {
            "INSTALL_BASE_URL": "http://127.0.0.1:8080", "SERVER_URL": SERVER_URL,
            "PROXY": self.proxy, "HTTP_PROXY": "", "HTTPS_PROXY": "", "ALL_PROXY": "",
            "http_proxy": "", "https_proxy": "", "all_proxy": "",
            "NO_PROXY": f"localhost,127.0.0.1,::1,{HOSTNAME}",
            "no_proxy": f"localhost,127.0.0.1,::1,{HOSTNAME}",
        }
        if role == "control":
            environment.update(BASE_DOMAIN="nodes.systemd.test", TLS_CERT_PATH=CERT, TLS_KEY_PATH=KEY,
                               DERP_REGION_ID="910", DERP_REGION_CODE="test", DERP_REGION_NAME="Test",
                               DERP_NODE_NAME="systemd-relay", DERP_HOSTNAME="relay.systemd.test")
        else:
            environment["NODE_HOSTNAME"] = NODE_NAME
        environment.update(settings)
        if authkey is not None:
            environment["NODE_AUTHKEY"] = authkey
        # Keep registration and proxy credentials out of command arguments and timeout errors
        return run(["docker", "exec", "-i", self.container, "python3", "-c",
                    "import json,os,sys; settings=json.load(sys.stdin); "
                    "os.execvpe('bash',['bash','/source/install.sh','--role',sys.argv[1]],{**os.environ,**settings})", role],
                   input=json.dumps(environment), timeout=1800, check=check)

    def pid(self, unit):
        return int(self.exec("systemctl", "show", unit, "--property=MainPID", "--value").stdout)

    def active(self, unit):
        self.wait(lambda: self.exec("systemctl", "is-active", "--quiet", unit, check=False).returncode == 0
                  and self.pid(unit) > 0, unit)
        self.exec("systemctl", "is-enabled", "--quiet", unit)

    def control_ready(self):
        self.active("headscale")
        self.wait(lambda: self.exec("curl", "-q", "-fsS", "--noproxy", "*", "--max-time", "3",
                                   f"{SERVER_URL}/health", check=False).returncode == 0, "control HTTPS")

    def node_ready(self):
        self.active("tailscaled")
        def connected():
            status = json.loads(self.exec("tailscale", "status", "--json").stdout)
            nodes = self.headscale("nodes", "list") or []
            return (status.get("BackendState") == "Running" and len(nodes) == 1
                    and nodes[0].get("online") is True
                    and str(nodes[0]["id"]) == str(status["Self"]["ID"]))
        self.wait(connected, "node connected to control", timeout=60)

    def headscale(self, *args):
        return json.loads(self.exec("headscale", *args, "--output", "json").stdout)

    def identity(self):
        status = json.loads(self.exec("tailscale", "status", "--json").stdout)
        require(status["BackendState"] == "Running", "Node is not Running")
        own = status["Self"]
        nodes = self.headscale("nodes", "list")
        require(len(nodes) == 1, "Registration created duplicate nodes")
        require(str(nodes[0]["id"]) == str(own["ID"]), "Control and node IDs differ")
        return {"noise": self.exec("sha256sum", "/var/lib/headscale/noise_private.key").stdout,
                "id": own["ID"], "key": own["PublicKey"], "ips": own["TailscaleIPs"]}

    def files(self):
        return self.exec("sha256sum", "/etc/headscale/config.yaml", "/etc/headscale/derp.yaml",
                         "/etc/headscale/policy.hujson", "/etc/default/tailscaled",
                         "/usr/lib/systemd/system/headscale.service",
                         "/lib/systemd/system/tailscaled.service").stdout

    def exit_advertisement(self, baseline):
        defaults = {"0.0.0.0/0", "::/0"}
        subnet = "192.0.2.0/24"
        self.exec("tailscale", "set", f"--advertise-routes={subnet}")

        def routes_match(expected):
            prefs = json.loads(self.exec("tailscale", "debug", "prefs").stdout)
            nodes = self.headscale("nodes", "list")
            return (set(prefs.get("AdvertiseRoutes") or []) == expected
                    and len(nodes) == 1 and set(nodes[0].get("available_routes") or []) == expected)

        self.install("node", ADVERTISE_EXIT_NODE="true")
        self.wait(lambda: routes_match(defaults | {subnet}), "exit advertisement reaches control")
        forwarding = self.exec("cat", "/etc/sysctl.d/99-tailbridge-exit-node.conf").stdout
        self.install("node")
        require(routes_match(defaults | {subnet}), "Unset exit setting withdrew existing routes")

        self.install("node", ADVERTISE_EXIT_NODE="false")
        self.wait(lambda: routes_match({subnet}), "exit withdrawal reaches control")
        self.exec("systemctl", "restart", "tailscaled")
        self.node_ready()
        self.install("node")
        require(routes_match({subnet}), "Restart or unset setting restored exit advertisement")
        require(self.identity() == baseline, "Exit toggle changed node identity")
        require(self.exec("cat", "/etc/sysctl.d/99-tailbridge-exit-node.conf").stdout == forwarding,
                "Exit withdrawal changed shared forwarding configuration")
        for key in ("net.ipv4.ip_forward", "net.ipv6.conf.all.forwarding"):
            require(self.exec("sysctl", "-n", key).stdout.strip() == "1", "Exit withdrawal disabled shared forwarding")

        self.install("node", ADVERTISE_EXIT_NODE="true")
        self.wait(lambda: routes_match(defaults | {subnet}), "exit re-enabled without registration")
        self.install("node", ADVERTISE_EXIT_NODE="false")
        self.wait(lambda: routes_match({subnet}), "exit withdrawn again")
        self.exec("tailscale", "set", "--advertise-routes=")
        passed("exit true/unset/false updates control routes, preserves subnets and identity, and survives restart")

    def verify(self):
        self.prepare()
        print("Installing control from official packages...", file=sys.stderr, flush=True)
        self.install("control")
        self.control_ready()
        uid = self.exec("id", "-u", "headscale").stdout.strip()
        owner = self.exec("stat", "-c", "%u", f"/proc/{self.pid('headscale')}").stdout.strip()
        require(owner == uid and uid != "0", "Headscale is not running as its service account")
        package = self.exec("dpkg-query", "-W", "-f=${Version}", "headscale").stdout
        require(self.exec("headscale", "version").stdout.splitlines()[0] == f"headscale version v{package}",
                "Headscale does not match the installed DEB")
        self.exec("runuser", "-u", "headscale", "--", "headscale", "configtest")
        passed("control installs the official DEB and enables non-root Headscale with trusted HTTPS")

        rejected = self.install("node", check=False)
        require(rejected.returncode == 2, "First registration did not require NODE_AUTHKEY")
        require(self.exec("test", "-e", "/var/lib/tailscale/tailscaled.state", check=False).returncode != 0,
                "Missing registration key created node state")
        passed("node rejects missing first-registration credentials before service startup")

        self.headscale("users", "create", "systemd")
        user = next(value for value in self.headscale("users", "list") if value["name"] == "systemd")
        key = self.headscale("preauthkeys", "create", "--user", str(user["id"]), "--expiration", "10m")["key"]
        print("Installing and registering node from official packages...", file=sys.stderr, flush=True)
        self.install("node", authkey=key)
        self.node_ready()
        for unit in ("headscale", "tailscaled"):
            fragment = self.exec("systemctl", "show", unit, "--property=FragmentPath", "--value").stdout.strip()
            require(fragment in {f"/lib/systemd/system/{unit}.service", f"/usr/lib/systemd/system/{unit}.service"},
                    f"Unexpected service file for {unit}")
            require(not self.exec("systemctl", "show", unit, "--property=DropInPaths", "--value").stdout.strip(),
                    f"Unexpected service overrides for {unit}")
        self.exec("ip", "link", "show", "tailscale0")
        baseline = self.identity()
        files = self.files()
        passed("node installs the official service, creates a TUN interface and registers with control")

        pids = [self.pid(unit) for unit in ("headscale", "tailscaled")]
        self.install("control")
        self.install("node", NODE_HOSTNAME="must-not-replace-saved-name")
        require([self.pid(unit) for unit in ("headscale", "tailscaled")] == pids, "Identical reinstall restarted services")
        require(self.identity() == baseline and self.files() == files, "Reinstall changed identities or configuration")
        prefs = json.loads(self.exec("tailscale", "debug", "prefs").stdout)
        require(prefs["Hostname"] == NODE_NAME and prefs["ControlURL"] == SERVER_URL, "Reinstall changed saved preferences")
        passed("control and node reinstall preserves service processes, configuration and registered identity")

        rejected = self.install("node", check=False, SERVER_URL="https://wrong.systemd.test")
        require(rejected.returncode == 2 and self.identity() == baseline, "Wrong control URL did not preserve identity")
        passed("node rejects a different control URL without re-registration")

        self.exit_advertisement(baseline)

        self.exec("tailscale", "down")
        require(json.loads(self.exec("tailscale", "status", "--json").stdout)["BackendState"] == "Stopped",
                "Node did not stop")
        self.install("node")
        self.node_ready()
        require(self.identity() == baseline, "Resuming the node replaced its identity")
        passed("node resumes saved registration from Stopped without an authentication key")

        for unit in ("headscale", "tailscaled"):
            previous_pid = self.pid(unit)
            self.exec("systemctl", "kill", "--signal=KILL", "--kill-whom=main", unit)
            self.wait(lambda: self.pid(unit) not in {0, previous_pid}, f"automatic {unit} restart")
            self.control_ready()
            self.node_ready()
            require(self.identity() == baseline, f"{unit} crash recovery changed identity")
            passed(f"systemd recovers {unit} after process failure with the same registered identity")

        run(["docker", "restart", "--time", "10", self.container], timeout=30)
        self.boot()
        self.control_ready()
        self.node_ready()
        require(self.identity() == baseline and self.files() == files, "Boot changed identity or configuration")
        passed("container boot starts control and node without installer environment or registration credentials")
        self.templates()
        pids = [self.pid(unit) for unit in ("headscale", "tailscaled")]
        self.install("control")
        self.install("node")
        require([self.pid(unit) for unit in ("headscale", "tailscaled")] == pids, "Reinstall after boot restarted services")
        require(self.identity() == baseline, "Reinstall after boot changed registration")
        passed("reinstall after boot preserves both services and node registration")


def main():
    parser = Parser(description=__doc__)
    parser.add_argument("--config", help="Existing E2E environment file")
    args = parser.parse_args()
    config = load_config(args.config)
    image = f"{config.project}-systemd-test"
    run(["docker", "build", "--build-arg", f"RUNTIME_IMAGE={config.project}-runtime",
         "-f", str(ROOT / "systemd.Dockerfile"), "-t", image, str(ROOT)], capture=False, timeout=600)
    container = f"tailbridge-control-node-service-{uuid.uuid4().hex[:12]}"
    attempted = False
    created = False
    try:
        attempted = True
        run(["docker", "create", "--name", container, "--privileged", "--cgroupns=private",
             "--network", "bridge", "--tmpfs", "/run", "--tmpfs", "/run/lock", "--tmpfs", "/tmp",
             "--add-host", f"{HOSTNAME}:127.0.0.1", "--add-host", "relay.systemd.test:127.0.0.1",
             "--add-host", "host.docker.internal:host-gateway",
             "--mount", f"type=bind,src={PROJECT_ROOT / 'install.sh'},dst=/source/install.sh,readonly",
             "--mount", f"type=bind,src={PROJECT_ROOT / 'files'},dst=/source/files,readonly", image])
        created = True
        run(["docker", "start", container])
        ControlNodeTest(container, config.env.get("TB_INSTALL_PROXY", "")).verify()
    except BaseException:
        if created:
            try:
                run(["docker", "exec", container, "journalctl", "-u", "headscale", "-u", "tailscaled",
                     "-n", "50", "--no-pager"], capture=False, check=False)
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                print(f"Warning: diagnostics unavailable for {container}", file=sys.stderr)
        raise
    finally:
        if attempted:
            failing = sys.exc_info()[0] is not None
            try:
                run(["docker", "rm", "--force", container], check=created)
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                if not failing:
                    raise
                print(f"Warning: could not remove test container {container}", file=sys.stderr)
    passed("isolated control/node container removed without changing existing E2E state")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Error: interrupted", file=sys.stderr)
        sys.exit(1)
