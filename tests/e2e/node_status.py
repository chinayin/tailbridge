"""等待 tailscaled 后端完成初始化；socket 出现不代表身份检查已完成。"""

import json
import subprocess
import sys
import time


def wait_backend(timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(['tailscale', 'status', '--json'], capture_output=True,
                                    text=True, timeout=1)
            state = json.loads(result.stdout).get('BackendState') if result.returncode == 0 else None
            if state in ('Running', 'Stopped', 'NeedsLogin', 'NeedsMachineAuth'):
                return state
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            pass
        time.sleep(0.2)
    raise RuntimeError('tailscaled backend startup timed out')


if __name__ == '__main__':
    try:
        print(wait_backend())
    except (OSError, RuntimeError) as error:
        print(f'Error: {error}', file=sys.stderr)
        sys.exit(1)
