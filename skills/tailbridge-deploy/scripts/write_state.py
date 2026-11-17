#!/usr/bin/env python3
"""Write a non-secret JSON snapshot from stdin as private, atomic YAML."""

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile

import yaml


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    if args.output.suffix not in (".yaml", ".yml"):
        parser.error("destination must be a YAML file")
    try:
        data = json.load(sys.stdin)
    except (ValueError, UnicodeError):
        print("Error: invalid JSON input", file=sys.stderr)
        return 1
    if not isinstance(data, dict):
        print("Error: expected a JSON object", file=sys.stderr)
        return 1
    output = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=args.output.parent,
            prefix="." + args.output.name + ".", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            os.fchmod(handle.fileno(), 0o600)
            handle.write(output)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, args.output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
