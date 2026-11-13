#!/usr/bin/env python3
"""Test DERP with systemd PID 1 in an isolated container. Exit codes: 0 pass, 1 fail."""

from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid

from e2e import Parser, run
from settings import load_config, PROJECT_ROOT, ROOT
from systemd_test import SystemdTest, passed, require

HOSTNAME = "derp.service.test"
CERT_DIR = "/etc/derper/certs"
CONFIG = "/etc/derper/derper.conf"
UNIT = "/etc/systemd/system/derper.service"
IDENTITY = "/var/lib/derper/derper.key"
PROXY_SERVER = r'''
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit

class Proxy(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        target = urlsplit(self.path)
        if target.scheme != "http" or target.netloc != "templates.invalid":
            self.send_error(502)
            return
        connection = HTTPConnection("127.0.0.1", 8080, timeout=5)
        try:
            connection.request("GET", target.path)
            response = connection.getresponse()
            data = response.read()
            self.send_response(response.status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            with open("/tmp/proxy-requests", "a") as log:
                log.write(target.path + "\n")
        finally:
            connection.close()

HTTPServer(("127.0.0.1", 8081), Proxy).serve_forever()
'''
PROBE = r'''
import os, socket, ssl, struct, sys, zlib
host, directory, port, closed_port = sys.argv[1:]
context = ssl.create_default_context(cafile=f"{directory}/{host}.crt")
with socket.create_connection(("127.0.0.1", int(port)), timeout=3) as tcp:
    with context.wrap_socket(tcp, server_hostname=host) as tls:
        tls.sendall(f"GET / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
        assert b" 200 " in tls.recv(1024).split(b"\r\n", 1)[0], "TLS HTTP request failed"
for number in {80, int(closed_port)} - {int(port)}:
    try:
        connection = socket.create_connection(("127.0.0.1", number), timeout=1)
    except ConnectionRefusedError:
        continue
    connection.close()
    raise AssertionError(f"Unexpected TCP listener: {number}")
transaction = os.urandom(12)
# Tailscale's net/stun parser requires SOFTWARE=tailnode and FINGERPRINT.
packet = struct.pack("!HHI", 1, 20, 0x2112a442) + transaction
packet += struct.pack("!HH", 0x8022, 8) + b"tailnode"
packet += struct.pack("!HHI", 0x8028, 4, zlib.crc32(packet) ^ 0x5354554e)
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
    udp.settimeout(3)
    udp.sendto(packet, ("127.0.0.1", 3478))
    response, _ = udp.recvfrom(1024)
assert response[:2] == b"\x01\x01" and response[8:20] == transaction, "Invalid STUN response"
'''


class ServiceTest(SystemdTest):
    def __init__(self, container, version):
        super().__init__(container)
        self.version = version

    def templates(self):
        run(["docker", "exec", "-d", self.container, "python3", "-m", "http.server", "8080",
             "--bind", "127.0.0.1", "--directory", "/source"])
        self.wait(lambda: self.exec("curl", "-q", "-fsS", "--noproxy", "*",
                                   "http://127.0.0.1:8080/files/derper.service", check=False).returncode == 0,
                  "template server")
        run(["docker", "exec", "-d", self.container, "python3", "-c", PROXY_SERVER])
        self.wait(lambda: self.exec("python3", "-c", "import socket; "
                                   "socket.create_connection(('127.0.0.1',8081),timeout=1).close()",
                                   check=False).returncode == 0, "local download proxy")

    def certificate(self, directory, hostname=HOSTNAME, *, san=True):
        self.exec("install", "-d", "-m", "755", directory)
        self.exec("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                  "-subj", f"/CN={hostname}",
                  *(["-addext", f"subjectAltName=DNS:{hostname}"] if san else []),
                  "-addext", "extendedKeyUsage=serverAuth", "-keyout", f"{directory}/{HOSTNAME}.key",
                  "-out", f"{directory}/{HOSTNAME}.crt")

    def install(self, *, port=443, cert_dir=CERT_DIR, force=False, check=True,
                proxy="", base_url="http://127.0.0.1:8080"):
        return self.exec("env", f"INSTALL_BASE_URL={base_url}", f"PROXY={proxy}",
                         "SERVER_URL=https://control.example.test", f"DERP_HOSTNAME={HOSTNAME}",
                         f"DERP_PORT={port}", f"DERP_CERT_DIR={cert_dir}",
                         "HTTP_PROXY=", "HTTPS_PROXY=", "ALL_PROXY=", "http_proxy=", "https_proxy=",
                         "all_proxy=", "NO_PROXY=localhost,127.0.0.1,::1",
                         "no_proxy=localhost,127.0.0.1,::1", "timeout", "45",
                         "bash", "/source/install.sh", "--role", "derp", "--tailscale-version", self.version,
                         *(["--force"] if force else []), check=check, timeout=50)

    def pid(self):
        return int(self.exec("systemctl", "show", "derper", "--property=MainPID", "--value").stdout)

    def state(self):
        hashes = self.exec("sha256sum", CONFIG, UNIT, IDENTITY, "/usr/local/bin/derper").stdout
        return self.pid(), hashes

    def probe(self, port, closed_port):
        self.exec("python3", "-c", PROBE, HOSTNAME, CERT_DIR, str(port), str(closed_port))
        return True

    def active(self, port=443, closed_port=8443):
        self.wait(lambda: self.exec("systemctl", "is-active", "--quiet", "derper", check=False).returncode == 0
                  and self.pid() > 0 and self.probe(port, closed_port), "DERP TLS and STUN")
        self.exec("systemctl", "is-enabled", "--quiet", "derper")
        uid = self.exec("id", "-u", "derper").stdout.strip()
        owner = self.exec("stat", "-c", "%u", f"/proc/{self.pid()}").stdout.strip()
        require(uid == owner and uid != "0", "DERP is not running as the derper account")
        arguments = self.exec("python3", "-c", "from pathlib import Path; import sys; "
                              "print(Path(sys.argv[1]).read_bytes().decode().replace(chr(0), chr(10)))",
                              f"/proc/{self.pid()}/cmdline").stdout.splitlines()
        for argument in (f"-hostname={HOSTNAME}", f"-a=:{port}", "-http-port=-1", "-stun=true",
                         "-stun-port=3478", "-certmode=manual", f"-certdir={CERT_DIR}", f"-c={IDENTITY}",
                         "-verify-client-url=https://control.example.test/verify",
                         "-verify-client-url-fail-open=false"):
            require(argument in arguments, f"Missing runtime argument: {argument}")

    def reject(self, label, expected_error, **options):
        previous = self.state()
        result = self.install(check=False, **options)
        require(result.returncode in {1, 2} and expected_error in result.stderr,
                f"{label} was not rejected as expected: {result.stderr[-1000:]}")
        require(self.state() == previous, f"{label} changed the active service, binary or identity")
        self.probe(443, 8443)
        passed(f"{label} preserves the running service")

    def verify(self):
        self.boot()
        self.templates()
        self.certificate(CERT_DIR)
        self.install(proxy="http://127.0.0.1:8081", base_url="http://templates.invalid")
        requests = self.exec("cat", "/tmp/proxy-requests").stdout.splitlines()
        require(requests == ["/files/derper.conf", "/files/derper.service"],
                "Installer downloads did not traverse the proxy")
        passed("installer downloads templates through PROXY without direct DNS or network access")
        self.active()
        identity = self.exec("sha256sum", IDENTITY).stdout
        passed("systemd enables non-root DERP with TLS 443, STUN 3478 and no TCP 80")

        previous = self.state()
        self.install(proxy="http://127.0.0.1:1")
        require(self.state() == previous, "Identical installation restarted DERP or replaced its state")
        require(self.exec("cat", "/tmp/proxy-requests").stdout.splitlines() == requests,
                "Loopback downloads unexpectedly used the proxy")
        passed("NO_PROXY keeps loopback template downloads direct even when the proxy is unavailable")
        passed("identical installation preserves the process, binary and identity")

        self.reject("changed port without --force", "use --force", port=8443)
        self.reject("missing certificate", "provide the DERP certificate", cert_dir="/missing", force=True)
        self.certificate("/tmp/wrong-host", "wrong.example.test")
        self.reject("wrong certificate hostname", "certificate", cert_dir="/tmp/wrong-host", force=True)
        self.certificate("/tmp/wrong-key")
        self.exec("cp", f"{CERT_DIR}/{HOSTNAME}.crt", f"/tmp/wrong-key/{HOSTNAME}.crt")
        self.reject("mismatched private key", "do not match", cert_dir="/tmp/wrong-key", force=True)
        self.certificate("/tmp/cn-only", san=False)
        self.reject("CN-only certificate", "certificate", cert_dir="/tmp/cn-only", force=True)

        self.install(port=8443, force=True)
        self.active(8443, 443)
        require(self.exec("sha256sum", IDENTITY).stdout == identity, "Port change replaced the DERP identity")
        passed("--force changes TLS to 8443 and closes 443 while preserving identity")

        unit = self.exec("sha256sum", UNIT).stdout
        self.exec("python3", "-c", "from pathlib import Path; import sys; "
                  "path = Path(sys.argv[1]); text = path.read_text(); "
                  "assert text.count('DERP_PORT=\"8443\"') == 1; "
                  "path.write_text(text.replace('DERP_PORT=\"8443\"', 'DERP_PORT=\"9443\"'))", CONFIG)
        self.exec("env", "-i", "/usr/bin/systemctl", "restart", "derper")
        self.active(9443, 8443)
        require(self.exec("sha256sum", UNIT).stdout == unit, "Configuration edit changed the service unit")
        require(self.exec("sha256sum", IDENTITY).stdout == identity, "Configuration edit replaced the DERP identity")
        configuration = self.exec("sha256sum", CONFIG).stdout
        passed("configuration alone changes TLS to 9443 after restart without installer or shell environment")

        previous_pid = self.pid()
        self.exec("systemctl", "kill", "--signal=KILL", "--kill-whom=main", "derper")
        self.wait(lambda: self.pid() not in {0, previous_pid}, "automatic DERP restart")
        self.active(9443, 8443)
        require(self.exec("sha256sum", IDENTITY).stdout == identity, "Crash recovery replaced the DERP identity")
        passed("systemd restarts a killed process with the same DERP identity")

        run(["docker", "restart", "--time", "10", self.container], timeout=30)
        self.boot()
        self.active(9443, 8443)
        require(self.exec("sha256sum", IDENTITY).stdout == identity, "Container restart replaced the DERP identity")
        require(self.exec("sha256sum", CONFIG).stdout == configuration, "Container restart changed the configuration")
        self.templates()
        previous = self.state()
        self.install(port=9443)
        require(self.state() == previous, "Installation after boot restarted DERP or replaced its state")
        passed("container boot preserves edited configuration, identity and installation idempotence")


def main():
    parser = Parser(description=__doc__)
    parser.add_argument("--config", type=Path, help="Existing E2E environment file")
    args = parser.parse_args()
    config = load_config(args.config)
    source = run([*config.compose, "ps", "--quiet", "derp-main"], env=config.env).stdout.strip()
    require(source, "Start the E2E DERP container before running this test")
    version = run(["docker", "exec", source, "/usr/local/bin/derper", "--version"]).stdout.strip()
    require(re.fullmatch(r"\d+\.\d+\.\d+(?:-ERR-BuildInfo)?", version), f"Unexpected derper version: {version}")
    version = version.removesuffix("-ERR-BuildInfo")
    image = f"{config.project}-systemd-test"
    run(["docker", "build", "--build-arg", f"RUNTIME_IMAGE={config.project}-runtime",
         "-f", str(ROOT / "systemd.Dockerfile"), "-t", image, str(ROOT)], capture=False, timeout=600)
    container = f"tailbridge-derper-service-{uuid.uuid4().hex[:12]}"
    attempted = False
    created = False
    try:
        with tempfile.TemporaryDirectory(prefix="tailbridge-derper-service-") as folder:
            binary = Path(folder) / "derper"
            run(["docker", "cp", f"{source}:/usr/local/bin/derper", str(binary)])
            attempted = True
            run(["docker", "create", "--name", container, "--privileged", "--cgroupns=private",
                 "--network", "none", "--tmpfs", "/run", "--tmpfs", "/run/lock", "--tmpfs", "/tmp",
                 "--mount", f"type=bind,src={PROJECT_ROOT / 'install.sh'},dst=/source/install.sh,readonly",
                 "--mount", f"type=bind,src={PROJECT_ROOT / 'files'},dst=/source/files,readonly", image])
            created = True
            run(["docker", "cp", str(binary), f"{container}:/usr/local/bin/derper"])
            run(["docker", "start", container])
            ServiceTest(container, version).verify()
    except BaseException:
        if created:
            for command in (["docker", "logs", "--tail", "40", container],
                            ["docker", "exec", container, "journalctl", "-u", "derper", "-n", "40", "--no-pager"]):
                try:
                    run(command, capture=False, check=False)
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
    passed("isolated test container and temporary files removed")


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"Error: {error}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("Error: interrupted", file=sys.stderr)
        sys.exit(1)
