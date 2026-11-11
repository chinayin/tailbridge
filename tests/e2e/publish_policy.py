"""在控制面容器内发布测试策略，避免跨宿主机文件视图的同步竞态。"""

import json
import os
from pathlib import Path
import sys
import tempfile


def write_policy(path, content):
    json.loads(content)
    fd, pending = tempfile.mkstemp(prefix='.policy.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            stream.write(content)
            stream.flush()
            os.fchmod(stream.fileno(), 0o644)
            os.fsync(stream.fileno())
        os.replace(pending, path)
    finally:
        Path(pending).unlink(missing_ok=True)


if __name__ == '__main__':
    try:
        write_policy(Path('/etc/headscale/policy.hujson'), sys.stdin.read())
    except (OSError, ValueError) as error:
        print(f'Error: {error}', file=sys.stderr)
        sys.exit(1)
