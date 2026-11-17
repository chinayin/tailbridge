#!/usr/bin/env bash
# Run offline installer tests with self-cleaning fixtures
set -uo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1
TB_PYTHON="${TB_PYTHON:-python3}"
if LC_ALL=C grep -nE '\$[A-Za-z_][A-Za-z0-9_]*[^ -~]' "${DIR}/install.sh" "${DIR}/test.sh"; then
    echo 'Error: use braces before non-ASCII characters in variable expansions' >&2
    exit 1
fi
status=0
"${TB_PYTHON}" -m unittest discover -s "${DIR}/tests/install" -p 'test_*.py' -v || status=1
"${TB_PYTHON}" -m unittest discover -s "${DIR}/skills/tailbridge-deploy/tests" -p 'test_*.py' -v || status=1
exit "${status}"
