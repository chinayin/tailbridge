"""Shared checks for isolated systemd test containers."""

import subprocess
import sys
import time

from e2e import run


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def passed(message):
    print(f"[PASS] {message}", file=sys.stderr, flush=True)


class SystemdTest:
    def __init__(self, container):
        self.container = container

    def exec(self, *args, **kwargs):
        return run(["docker", "exec", self.container, *args], **kwargs)

    def wait(self, check, description, timeout=30):
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            try:
                if check():
                    return
            except (RuntimeError, subprocess.TimeoutExpired) as error:
                last_error = str(error)
            time.sleep(0.5)
        raise RuntimeError(f"Timed out waiting for {description}: {last_error}")

    def boot(self):
        self.wait(lambda: self.exec("systemctl", "is-system-running", check=False).stdout.strip()
                  in {"running", "degraded"}, "systemd boot")
        require(self.exec("cat", "/proc/1/comm").stdout.strip() == "systemd", "PID 1 is not systemd")
        self.exec("systemctl", "start", "systemd-journald")
