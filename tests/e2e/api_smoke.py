"""Run inside a client container; receive a short-lived management key on stdin."""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import secrets
import signal
import ssl
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def require(condition, message):
    if not condition:
        raise AssertionError(message)


def request(base, key, method, path, body=None, expected=200):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    data = json.dumps(body).encode() if body is not None else None
    tls = ssl.create_default_context(cafile="/usr/local/share/ca-certificates/tailbridge.crt")
    # The local OpenSSL fixtures omit AKI; keep CA and hostname verification enabled.
    tls.verify_flags &= ~ssl.VERIFY_X509_STRICT
    try:
        with urlopen(Request(base.rstrip("/") + "/api/v1" + path, data=data,
                             headers=headers, method=method), context=tls, timeout=10) as response:
            status, content = response.status, response.read()
    except HTTPError as error:
        status, content = error.code, error.read()
    require(status == expected, f"REST {method} {path}: HTTP {status}, expected {expected}")
    return json.loads(content) if expected == 200 else None


def cleanup(api, user_id):
    errors = []
    for path, field in (("/node", "nodes"), ("/preauthkey", "preAuthKeys")):
        try:
            resources = api("GET", path).get(field, [])
            owned = [item for item in resources
                     if str((item.get("user") or {}).get("id")) == str(user_id)]
            for item in owned:
                try:
                    suffix = f"/{item['id']}" if path == "/node" else f"?id={item['id']}"
                    api("DELETE", path + suffix)
                except Exception:
                    errors.append(f"delete {path} ID {item['id']}")
            remaining = {item["id"] for item in api("GET", path).get(field, [])}
            require(not remaining.intersection(item["id"] for item in owned), "Temporary resources remain")
        except Exception:
            errors.append(f"list {path}")
    try:
        api("DELETE", f"/user/{user_id}")
        require(not api("GET", f"/user?id={user_id}").get("users"), "Temporary user remains")
    except Exception:
        errors.append(f"delete user ID {user_id}")
    require(not errors, "REST cleanup failed: " + "; ".join(errors))


def smoke(base, key):
    def api(method, path, body=None):
        return request(base, key, method, path, body)

    def snapshot():
        return {str(item["id"]): {field: item.get(field) for field in
                ("nodeKey", "ipAddresses", "givenName", "user", "tags", "approvedRoutes", "expiry")}
                for item in api("GET", "/node").get("nodes", [])}

    request(base, "", "GET", "/user", expected=401)
    request(base, "invalid", "GET", "/user", expected=401)
    baseline = snapshot()
    name = "api-smoke-" + secrets.token_hex(6)
    require(not api("GET", f"/user?name={name}").get("users"), "Temporary username already exists")
    user_id = None
    checks = ["REST rejects absent and invalid API keys"]
    with tempfile.TemporaryDirectory(prefix="tailbridge-api-") as directory:
        folder = Path(directory)
        daemon = None
        try:
            user_id = api("POST", "/user", {"name": name})["user"]["id"]
            renamed = name + "-renamed"
            api("POST", f"/user/{user_id}/rename/{renamed}")
            require(api("GET", f"/user?id={user_id}")["users"][0]["name"] == renamed,
                    "User rename was not persisted")
            expiration = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
            auth = api("POST", "/preauthkey", {"user": user_id, "expiration": expiration,
                                               "reusable": False, "ephemeral": False})["preAuthKey"]
            auth_file = folder / "authkey"
            auth_file.write_text(auth["key"])
            auth_file.chmod(0o600)
            sock = folder / "tailscaled.sock"
            daemon = subprocess.Popen(["tailscaled", "--tun=userspace-networking", "--port=0",
                                       f"--socket={sock}", f"--statedir={folder / 'state'}"],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 10
            while not sock.exists() and time.monotonic() < deadline and daemon.poll() is None:
                time.sleep(0.1)
            require(sock.exists(), "Temporary tailscaled did not start")
            result = subprocess.run(["tailscale", f"--socket={sock}", "up", f"--login-server={base}",
                                     f"--auth-key=file:{auth_file}", f"--hostname={name}",
                                     "--accept-dns=false", "--accept-routes=false", "--advertise-exit-node",
                                     "--timeout=30s"], capture_output=True, timeout=40)
            require(result.returncode == 0, "Temporary node registration failed")
            auth_file.unlink()
            nodes = [item for item in api("GET", "/node").get("nodes", [])
                     if str((item.get("user") or {}).get("id")) == str(user_id)]
            require(len(nodes) == 1, "Temporary user must own exactly one registered node")
            node_id = nodes[0]["id"]
            path = f"/node/{node_id}"
            require(nodes[0]["ipAddresses"], "Temporary node has no assigned address")
            require(set(nodes[0].get("availableRoutes", [])) == {"0.0.0.0/0", "::/0"},
                    "Temporary node did not advertise both exit routes")
            checks.append("REST user create/list/rename and preauthkey register a real temporary node")
            api("POST", path + f"/rename/{renamed}")
            require(api("GET", path)["node"]["givenName"] == renamed, "Node rename was not persisted")
            api("POST", path + "/approve_routes", {"routes": ["0.0.0.0/0", "::/0"]})
            require(set(api("GET", path)["node"]["approvedRoutes"]) == {"0.0.0.0/0", "::/0"},
                    "Exit route approval was not persisted")
            api("POST", path + "/approve_routes", {"routes": []})
            require(not api("GET", path)["node"].get("approvedRoutes"), "Route revocation was not persisted")
            checks.append("REST node read/rename and dual-stack route approval/revocation")
            api("POST", "/preauthkey/expire", {"id": auth["id"]})
            expired = next(item for item in api("GET", "/preauthkey")["preAuthKeys"]
                           if item["id"] == auth["id"])
            require(datetime.fromisoformat(expired["expiration"].replace("Z", "+00:00"))
                    <= datetime.now(timezone.utc), "Preauthkey did not expire")
            status = subprocess.run(["tailscale", f"--socket={sock}", "status", "--json"],
                                    capture_output=True, text=True, timeout=10)
            require(status.returncode == 0 and json.loads(status.stdout)["BackendState"] == "Running",
                    "Expiring registration key invalidated the registered node")
            api("POST", path + "/expire")
            require(datetime.fromisoformat(api("GET", path)["node"]["expiry"].replace("Z", "+00:00"))
                    <= datetime.now(timezone.utc), "Node did not expire")
            checks.append("REST preauthkey expiration preserves registration; node expiration is separate")
        finally:
            signal.alarm(0)
            try:
                if daemon:
                    daemon.terminate()
                    try:
                        daemon.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        daemon.kill()
                        daemon.wait(timeout=5)
            finally:
                if user_id is None:
                    users = api("GET", f"/user?name={name}").get("users", [])
                    user_id = users[0]["id"] if users else None
                if user_id is not None:
                    cleanup(api, user_id)
    require(snapshot() == baseline, "Existing node identities or approved routes changed")
    checks.append("REST deletes temporary nodes, preauthkeys and user; existing nodes and routes preserved")
    return {"checks": checks}


if __name__ == "__main__":
    def interrupted(signum, frame):
        raise TimeoutError("REST smoke interrupted")

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGALRM, interrupted)
    signal.alarm(120)
    try:
        print(json.dumps(smoke(sys.argv[1], sys.stdin.read().strip())))
    except Exception as error:
        # Never print HTTP bodies, command output or credentials.
        message = str(error) if isinstance(error, AssertionError) else type(error).__name__
        print(f"Error: {message}", file=sys.stderr)
        sys.exit(1)
